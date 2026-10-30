# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for project visibility on the issue relation list.

``GET .../issues/<issue_id>/issue-relation/`` returns the related issues of the URL
issue. Relations legitimately cross projects, but the related issues must only be
those the caller may view: issues of projects where they are an active member, and,
for a GUEST on a project with ``guest_view_all_features`` disabled, only the issues
they created (the rule ``_visible_issues_q`` and the external API's relation list apply).

``ProjectEntityPermission`` and ``issue_hidden_from_guest`` only look at the URL
project, so a related issue living in a project the caller is not a member of used to
be returned with its name, priority, assignees and labels.
"""

from uuid import uuid4

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import Issue, IssueRelation, Project, ProjectMember, User, WorkspaceMember

RELATION_URL = "/api/workspaces/{slug}/projects/{project_id}/issues/{issue_id}/issue-relation/"

# (stored relation_type, URL issue is the ``issue`` side, key the list response puts it under)
RELATION_CASES = [
    ("blocked_by", True, "blocked_by"),
    ("blocked_by", False, "blocking"),
    ("duplicate", True, "duplicate"),
    ("duplicate", False, "duplicate"),
    ("relates_to", True, "relates_to"),
    ("relates_to", False, "relates_to"),
    ("start_before", True, "start_before"),
    ("start_before", False, "start_after"),
    ("finish_before", True, "finish_before"),
    ("finish_before", False, "finish_after"),
]


def _make_user(workspace, workspace_role):
    user = User.objects.create(email=f"user-{uuid4().hex[:8]}@plane.so", username=f"user_{uuid4().hex[:8]}")
    user.set_password("test-password")
    user.save()
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=workspace_role)
    return user


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _make_issue(name, project, workspace, author):
    """Create an issue with a deterministic ``created_by`` (BaseModel.save
    otherwise nulls it under tests)."""
    issue = Issue(name=name, project=project, workspace=workspace)
    issue.save(created_by_id=author.id)
    return issue


def _relate(url_issue, other_issue, relation_type="relates_to", url_issue_is_issue_side=True):
    issue, related_issue = (url_issue, other_issue) if url_issue_is_issue_side else (other_issue, url_issue)
    return IssueRelation.objects.create(
        workspace=url_issue.workspace,
        project=issue.project,
        issue=issue,
        related_issue=related_issue,
        relation_type=relation_type,
    )


def _list(user, workspace, project, issue):
    return _client(user).get(RELATION_URL.format(slug=workspace.slug, project_id=project.id, issue_id=issue.id))


def _related_ids(response):
    return {str(row["id"]) for rows in response.data.values() for row in rows}


@pytest.fixture
def project_a(db, workspace, create_user):
    """The URL project. ``create_user`` (workspace admin) is its project ADMIN."""
    project = Project.objects.create(name="Project A", identifier="PA", workspace=workspace, created_by=create_user)
    ProjectMember.objects.create(project=project, member=create_user, workspace=workspace, role=20)
    return project


@pytest.fixture
def project_b(db, workspace, create_user):
    """A sibling project in the same workspace. ``create_user`` is its project ADMIN."""
    project = Project.objects.create(name="Project B", identifier="PB", workspace=workspace, created_by=create_user)
    ProjectMember.objects.create(project=project, member=create_user, workspace=workspace, role=20)
    return project


@pytest.fixture
def guest(db, workspace, project_a):
    """An active GUEST (role 5) of project A only."""
    user = _make_user(workspace, 5)
    ProjectMember.objects.create(project=project_a, member=user, workspace=workspace, role=5)
    return user


@pytest.fixture
def issue_a1(db, workspace, project_a, guest):
    """The guest's own issue in A, so a restricted guest may open it."""
    return _make_issue("A1", project_a, workspace, guest)


@pytest.fixture
def issue_a2(db, workspace, project_a, guest):
    """A second issue in A that the guest also created."""
    return _make_issue("A2", project_a, workspace, guest)


@pytest.fixture
def issue_b1(db, workspace, project_b, create_user):
    """An issue in B, which the guest is not a member of."""
    return _make_issue("B1", project_b, workspace, create_user)


@pytest.mark.contract
@pytest.mark.django_db
class TestIssueRelationProjectVisibility:
    # ---- a) a guest never sees related issues of a project they are not in ----

    @pytest.mark.parametrize("relation_type,url_issue_is_issue_side,key", RELATION_CASES)
    def test_guest_does_not_see_related_issue_of_non_member_project(
        self, workspace, project_a, guest, issue_a1, issue_b1, relation_type, url_issue_is_issue_side, key
    ):
        _relate(issue_a1, issue_b1, relation_type, url_issue_is_issue_side)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK, f"Got {response.status_code}: {response.data!r}"
        assert str(issue_b1.id) not in _related_ids(response), response.data
        assert list(response.data[key]) == []

    def test_guest_with_view_all_does_not_see_non_member_project(self, workspace, project_a, guest, issue_a1, issue_b1):
        """``guest_view_all_features`` on A widens what the guest sees *in A* only."""
        Project.objects.filter(pk=project_a.pk).update(guest_view_all_features=True)
        _relate(issue_a1, issue_b1)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert str(issue_b1.id) not in _related_ids(response), response.data

    def test_guest_does_not_see_project_they_left(self, workspace, project_a, project_b, guest, issue_a1, issue_b1):
        ProjectMember.objects.create(project=project_b, member=guest, workspace=workspace, role=5, is_active=False)
        _relate(issue_a1, issue_b1)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert str(issue_b1.id) not in _related_ids(response), response.data

    # ---- b) relations inside the guest's own project still appear ----

    def test_guest_sees_related_issue_in_own_project(self, workspace, project_a, guest, issue_a1, issue_a2):
        _relate(issue_a1, issue_a2)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert _related_ids(response) == {str(issue_a2.id)}

    def test_guest_only_loses_the_hidden_side_of_mixed_relations(
        self, workspace, project_a, guest, issue_a1, issue_a2, issue_b1
    ):
        _relate(issue_a1, issue_a2)
        _relate(issue_a1, issue_b1)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert _related_ids(response) == {str(issue_a2.id)}

    def test_restricted_guest_does_not_see_issues_they_did_not_create_in_own_project(
        self, workspace, project_a, guest, issue_a1, create_user
    ):
        """Same per-issue rule as issue detail: a restricted guest only sees their own issues."""
        foreign = _make_issue("A-foreign", project_a, workspace, create_user)
        _relate(issue_a1, foreign)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert str(foreign.id) not in _related_ids(response), response.data

    def test_guest_with_view_all_sees_all_related_issues_in_own_project(
        self, workspace, project_a, guest, issue_a1, create_user
    ):
        Project.objects.filter(pk=project_a.pk).update(guest_view_all_features=True)
        foreign = _make_issue("A-foreign", project_a, workspace, create_user)
        _relate(issue_a1, foreign)

        response = _list(guest, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert _related_ids(response) == {str(foreign.id)}

    # ---- c) a member of both projects sees both ----

    @pytest.mark.parametrize("relation_type,url_issue_is_issue_side,key", RELATION_CASES)
    def test_member_of_both_projects_sees_both(
        self, workspace, project_a, project_b, issue_a1, issue_a2, issue_b1, relation_type, url_issue_is_issue_side, key
    ):
        member = _make_user(workspace, 15)
        ProjectMember.objects.create(project=project_a, member=member, workspace=workspace, role=15)
        ProjectMember.objects.create(project=project_b, member=member, workspace=workspace, role=15)
        _relate(issue_a1, issue_a2, relation_type, url_issue_is_issue_side)
        _relate(issue_a1, issue_b1, relation_type, url_issue_is_issue_side)

        response = _list(member, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK, f"Got {response.status_code}: {response.data!r}"
        assert {str(row["id"]) for row in response.data[key]} == {str(issue_a2.id), str(issue_b1.id)}

    # ---- question: a non-guest member is held to the same project boundary ----

    def test_member_does_not_see_related_issue_of_non_member_project(
        self, workspace, project_a, issue_a1, issue_a2, issue_b1
    ):
        member = _make_user(workspace, 15)
        ProjectMember.objects.create(project=project_a, member=member, workspace=workspace, role=15)
        _relate(issue_a1, issue_a2)
        _relate(issue_a1, issue_b1)

        response = _list(member, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert _related_ids(response) == {str(issue_a2.id)}

    # ---- d) workspace admins ----

    def test_workspace_admin_member_of_both_projects_sees_both(
        self, workspace, project_a, project_b, issue_a1, issue_a2, issue_b1
    ):
        admin = _make_user(workspace, 20)
        ProjectMember.objects.create(project=project_a, member=admin, workspace=workspace, role=15)
        ProjectMember.objects.create(project=project_b, member=admin, workspace=workspace, role=15)
        _relate(issue_a1, issue_a2)
        _relate(issue_a1, issue_b1)

        response = _list(admin, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert _related_ids(response) == {str(issue_a2.id), str(issue_b1.id)}

    def test_workspace_admin_gets_no_project_bypass(self, workspace, project_a, issue_a1, issue_a2, issue_b1):
        """Workspace admin only counts as an admin on projects they are a member of
        (``allow_permission``, ``ProjectEntityPermission``, ``_visible_issues_q``), so a
        project B they never joined stays hidden."""
        admin = _make_user(workspace, 20)
        ProjectMember.objects.create(project=project_a, member=admin, workspace=workspace, role=15)
        _relate(issue_a1, issue_a2)
        _relate(issue_a1, issue_b1)

        response = _list(admin, workspace, project_a, issue_a1)

        assert response.status_code == status.HTTP_200_OK
        assert _related_ids(response) == {str(issue_a2.id)}
