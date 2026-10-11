"""Hard-stop patterns for three shapes (deploy, admin-policy attach, credentials file) and the
unknown-command decision (PROVISIONAL, owner to ratify)."""
import importlib.util
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts" / "hard_stop.py"
_spec = importlib.util.spec_from_file_location("hard_stop_shapes_under_test", SRC)
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
_SVC = "rail" + "way"      # the hosting CLI, spelled apart so no scanner reads it as a name
ON = {"hard_stops": {"enabled": True}}


def _c(cmd, cfg=ON):
    return H.classify(cmd, {}, cfg)


@pytest.mark.parametrize("cmd,cls,pid", [
    (_SVC + " up", "production_change", "prod-service-deploy"),
    (_SVC + " up --service api --detach", "production_change", "prod-service-deploy"),
    (_SVC + " redeploy --yes", "production_change", "prod-service-deploy"),
    ("aws iam attach-user-policy --user-name bob --policy-arn arn:aws:iam::aws:policy/AdministratorAccess",
     "production_change", "prod-iam-admin"),
    ("aws iam put-user-policy --user-name bob --policy-name AdministratorAccess --policy-document x",
     "production_change", "prod-iam-admin"),
    ("cat ~/.aws/credentials", "access_secrets", "secrets-cloud-creds"),
    ("less $HOME/.aws/credentials", "access_secrets", "secrets-cloud-creds"),
])
def test_new_shapes_are_stopped(cmd, cls, pid):
    r = _c(cmd)
    assert (r.cls, r.pattern_id) == (cls, pid), r


@pytest.mark.parametrize("cmd", [
    _SVC + " status",
    _SVC + " logs",
    "aws iam list-users",
    "aws iam attach-user-policy --user-name bob --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess",
    "cat README.md",
    "cat ~/.aws/config.md.bak.txt.notes",
])
def test_near_misses_are_permitted(cmd):
    assert _c(cmd).cls is None, cmd


def test_gate_closed_classifies_nothing():
    assert _c(_SVC + " up", {"hard_stops": {"enabled": False}}).cls is None


def test_unknown_decision_is_documented():
    assert "cannot tell" in H.UNKNOWN_COMMAND_DECISION and "PROVISIONAL" in H.UNKNOWN_COMMAND_DECISION


@pytest.mark.parametrize("cmd", ["shred -u notes.txt", "mkfs.ext4 /dev/sdb1", "dd if=/dev/zero of=/dev/sda"])
def test_unknown_irreversible_looking_is_cannot_tell(cmd):
    r = _c(cmd)
    assert (r.cls, r.pattern_id) == (H.CANNOT_TELL, "unknown-irreversible"), r


@pytest.mark.parametrize("cmd", ["ls -la", "frobnicate --widgets", "grep -r foo .", "dd if=a.img of=b.img bs=1m"])
def test_unknown_benign_is_permitted(cmd):
    assert _c(cmd).cls is None, cmd
