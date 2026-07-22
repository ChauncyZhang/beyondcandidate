from uuid import UUID
from io import BytesIO

import pytest

from server.app.identity.policy import Principal
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.http import content_disposition, derive_cursor_key
from server.app.recruiting.storage import MinioResumeStorage, StorageObjectTooLarge, StorageReadFailed


def principal(role: str) -> Principal:
    return Principal(UUID(int=1), UUID(int=2), frozenset({role}), True)


@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    [
        ("recruiting_admin", action, True) for action in RecruitingAction
    ] + [
        ("recruiter", RecruitingAction.READ, True),
        ("recruiter", RecruitingAction.COMMENT, True),
        ("recruiter", RecruitingAction.MANAGE_CANDIDATE, True),
        ("recruiter", RecruitingAction.MANAGE_JOB, True),
        ("recruiter", RecruitingAction.CREATE_VERSION, True),
        ("recruiter", RecruitingAction.TRANSITION, True),
        ("recruiter", RecruitingAction.PREVIEW, True),
        ("recruiter", RecruitingAction.ISSUE_TICKET, True),
        ("recruiter", RecruitingAction.DOWNLOAD, True),
        ("recruiter", RecruitingAction.EXPORT, True),
        ("recruiter", RecruitingAction.RECOMMEND, True),
        ("hiring_manager", RecruitingAction.READ, True),
        ("hiring_manager", RecruitingAction.COMMENT, True),
        ("hiring_manager", RecruitingAction.RECOMMEND, True),
        ("hiring_manager", RecruitingAction.MANAGE_CANDIDATE, False),
        ("hiring_manager", RecruitingAction.MANAGE_JOB, False),
        ("hiring_manager", RecruitingAction.CREATE_VERSION, False),
        ("hiring_manager", RecruitingAction.TRANSITION, False),
        ("hiring_manager", RecruitingAction.PREVIEW, True),
        ("hiring_manager", RecruitingAction.ISSUE_TICKET, False),
        ("hiring_manager", RecruitingAction.DOWNLOAD, False),
        ("hiring_manager", RecruitingAction.EXPORT, False),
    ] + [
        (role, action, False)
        for role in ("system_admin", "interviewer")
        for action in RecruitingAction
    ],
)
def test_action_matrix_is_centralized(role, action, allowed):
    assert RecruitingAuthorizationService.role_allows(principal(role), action) is allowed


@pytest.mark.parametrize(
    ("filename", "expected_ascii", "expected_encoded"),
    [
        ("resume.pdf", "resume.pdf", "resume.pdf"),
        ('a"b.pdf', "a_b.pdf", "a%22b.pdf"),
        ("../folder/resume.pdf", "resume.pdf", "resume.pdf"),
        ("候选人.pdf", "download.pdf", "%E5%80%99%E9%80%89%E4%BA%BA.pdf"),
        ("x" * 400 + ".pdf", "x" * 115 + ".pdf", "x" * 115 + ".pdf"),
    ],
)
def test_content_disposition_has_safe_ascii_and_rfc5987(filename, expected_ascii, expected_encoded):
    value = content_disposition(filename)
    assert value == f"attachment; filename=\"{expected_ascii}\"; filename*=UTF-8''{expected_encoded}"


@pytest.mark.parametrize("filename", ["bad\rname.pdf", "bad\nname.pdf", "bad\x00name.pdf", "\x1fname.pdf"])
def test_content_disposition_rejects_controls(filename):
    with pytest.raises(ValueError):
        content_disposition(filename)


def test_cursor_key_uses_domain_separated_hkdf():
    source = b"a" * 32
    key = derive_cursor_key(source)
    assert len(key) == 32
    assert key != source
    assert key == derive_cursor_key(source)


class StorageResponse:
    def __init__(self, chunks, failure_at=None):
        self.chunks = chunks
        self.failure_at = failure_at
        self.closed = False
        self.released = False

    def stream(self, _):
        for index, chunk in enumerate(self.chunks):
            if index == self.failure_at:
                raise OSError("upstream failed")
            yield chunk

    def close(self): self.closed = True
    def release_conn(self): self.released = True


class StorageClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get_object(self, bucket, key):
        self.calls.append((bucket, key))
        if self.error: raise self.error
        return self.response


def test_minio_storage_spools_successful_multi_chunk_and_closes_upstream():
    upstream = StorageResponse([b"one", b"two", b"three"])
    client = StorageClient(upstream)
    spool = MinioResumeStorage(client, "private").open_download("objects/id", max_bytes=20)
    assert spool.read() == b"onetwothree"
    assert client.calls == [("private", "objects/id")]
    assert upstream.closed and upstream.released
    spool.close()


def test_minio_storage_open_failure_is_redacted():
    with pytest.raises(StorageReadFailed):
        MinioResumeStorage(StorageClient(error=OSError("secret endpoint")), "private").open_download("objects/id")


@pytest.mark.parametrize(
    ("upstream", "error"),
    [
        (StorageResponse([b"one", b"two"], failure_at=1), StorageReadFailed),
        (StorageResponse([b"12345", b"67890"]), StorageObjectTooLarge),
    ],
)
def test_minio_storage_mid_read_and_oversize_close_every_resource(upstream, error):
    with pytest.raises(error):
        MinioResumeStorage(StorageClient(upstream), "private").open_download("objects/id", max_bytes=8)
    assert upstream.closed and upstream.released
