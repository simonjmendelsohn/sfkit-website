import pytest
from src.utils.google_cloud.google_cloud_iam import GoogleCloudIAM


@pytest.mark.parametrize(("role"), (("roles/logging.viewer"), ("anotherrole")))
def test_give_cloud_build_view_permissions(mocker, role):
    mocker.patch(
        "src.utils.google_cloud.google_cloud_iam.googleapi",
        MockMakeMockIam,
    )
    mocker.patch(
        __name__ + ".MockExecutable.execute",
        return_value={
            "bindings": [
                {"role": role, "members": ["0"]},
            ]
        },
    )

    google_cloud_iam = GoogleCloudIAM()
    google_cloud_iam.give_cloud_build_view_permissions("user")


def test_test_permissions(mocker):
    mocker.patch(
        "src.utils.google_cloud.google_cloud_iam.googleapi",
        MockMakeMockIam,
    )

    google_cloud_iam = GoogleCloudIAM()
    assert google_cloud_iam.test_permissions("project") == False


class MockMakeMockIam:
    def build(api, version):
        return MockIam()


class MockIam:
    def projects(self):
        return MockProjects()


class MockProjects:
    def getIamPolicy(self, resource, body):
        return MockExecutable()

    def setIamPolicy(self, resource, body):
        return MockExecutable()

    def testIamPermissions(self, resource, body):
        return MockExecutable()


class MockExecutable:
    def execute(self):
        return {"pemrissions": []}