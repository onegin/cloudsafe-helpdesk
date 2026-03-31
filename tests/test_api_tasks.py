import os
import tempfile
import unittest


_db_fd, _db_path = tempfile.mkstemp(prefix="helpdesk_test_", suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["CSRF_ENABLED"] = "0"

from app import create_app  # noqa: E402
from models import (  # noqa: E402
    OperatorOrganizationAccess,
    Organization,
    Priority,
    Roles,
    Status,
    User,
    db,
)


class ApiTaskCreationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.close(_db_fd)
        if os.path.exists(_db_path):
            os.unlink(_db_path)

    def setUp(self):
        with self.app.app_context():
            db.session.query(OperatorOrganizationAccess).delete()
            db.session.query(User).delete()
            db.session.query(Status).delete()
            db.session.query(Priority).delete()
            db.session.query(Organization).delete()
            db.session.commit()

            self.org1 = Organization(name="Org 1")
            self.org2 = Organization(name="Org 2")
            db.session.add_all([self.org1, self.org2])
            db.session.flush()

            self.priority = Priority(name="Высокий", color="#dc3545", sort_order=30, is_default=True)
            self.status = Status(name="Новая", sort_order=10, is_final=False)
            self.admin = User(username="admin_test", role=Roles.ADMIN, active=True, email="admin@test.local")
            self.admin.set_password("pass")

            self.operator_allowed = User(
                username="operator_allowed",
                role=Roles.OPERATOR,
                active=True,
                email="op1@test.local",
            )
            self.operator_allowed.set_password("pass")

            self.operator_blocked = User(
                username="operator_blocked",
                role=Roles.OPERATOR,
                active=True,
                email="op2@test.local",
            )
            self.operator_blocked.set_password("pass")

            db.session.add_all([
                self.priority,
                self.status,
                self.admin,
                self.operator_allowed,
                self.operator_blocked,
            ])
            db.session.flush()

            db.session.add(
                OperatorOrganizationAccess(
                    operator_id=self.operator_allowed.id,
                    organization_id=self.org1.id,
                )
            )

            self.org1_token = self.org1.generate_api_token()
            self.priority_id = self.priority.id
            self.org2_id = self.org2.id
            self.operator_allowed_id = self.operator_allowed.id
            self.operator_blocked_id = self.operator_blocked.id
            db.session.commit()

    def test_api_task_create_validates_org_and_assignee_access(self):
        payload = {
            "theme": "Падает VPN",
            "content": "Нужна проверка",
            "due_date": "2026-12-31",
            "priority_id": self.priority_id,
            "assigned_to_id": self.operator_allowed_id,
        }

        response = self.client.post(
            "/api/tasks",
            json=payload,
            headers={"Authorization": f"Bearer {self.org1_token}"},
        )
        self.assertEqual(response.status_code, 201)

        mismatch_org_payload = dict(payload)
        mismatch_org_payload["organization_id"] = self.org2_id
        mismatch_response = self.client.post(
            "/api/tasks",
            json=mismatch_org_payload,
            headers={"Authorization": f"Bearer {self.org1_token}"},
        )
        self.assertEqual(mismatch_response.status_code, 403)

        blocked_assignee_payload = dict(payload)
        blocked_assignee_payload["assigned_to_id"] = self.operator_blocked_id
        blocked_assignee_response = self.client.post(
            "/api/tasks",
            json=blocked_assignee_payload,
            headers={"Authorization": f"Bearer {self.org1_token}"},
        )
        self.assertEqual(blocked_assignee_response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
