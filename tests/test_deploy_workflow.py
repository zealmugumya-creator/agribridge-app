# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

"""Regression: the Cloudflare SPA deploy workflow must never report success while
deploying nothing.

Root cause (verified live 2026-10-22): the run for push `6077883` finished
`success` but its "Deploy Worker static assets" step was `skipped`, because the
old workflow guarded the deploy on `CLOUDFLARE_API_TOKEN != ''` and, on the empty
path, only printed a warning. A green deploy run therefore did not prove a
release shipped — which is exactly why the bank-transfer checkout silently never
appeared. These tests lock the contract: the deploy step must fail (exit 1) when
the token is missing, and must still publish when it is present.

Fails against the original workflow, passes after the fix.
"""
from __future__ import annotations

import pathlib
import re

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/deploy-cloudflare.yml"


def _text():
    assert WORKFLOW.exists(), f"missing workflow file {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


def test_deploy_step_fails_loudly_when_token_is_missing():
    text = _text()
    # The previous silent-skip structure must be gone: the deploy must not be a
    # step that simply does not run when the token is absent.
    assert 'if: ${{ env.CLOUDFLARE_API_TOKEN != \x27\x27 }}' not in text, (
        "deploy is still a skipped step; a missing token would leave the run green")

    # There must be a hard failure (exit 1) tied to an empty-token guard.
    assert re.search(r'-z\s+"?\$CLOUDFLARE_API_TOKEN', text), (
        "no empty-token guard found")
    assert "exit 1" in text, "workflow must exit non-zero when it cannot deploy"


def test_deploy_step_still_publishes_when_token_present():
    # The happy path is preserved: wrangler actually deploys.
    assert "wrangler@4 deploy" in _text()


def test_no_warning_only_skip_step_remains():
    # The old step reported the missing credential but let the job finish green.
    # Whatever reports the problem must be paired with a non-zero exit, not a bare warning.
    text = _text()
    if "Report missing deployment credential" in text:
        # If that step name is kept for messaging, the run block must still fail.
        assert "exit 1" in text


def test_wrangler_failure_reason_is_visible_without_dashboard_access():
    """A red deploy run must say WHY it is red in the run's annotations.

    Job logs are only readable by an authenticated collaborator, so the push at
    `20f45f2` failed with nothing but "Process completed with exit code 1" and
    the cause could not be established from outside the account. The workflow
    must therefore capture wrangler's output, re-publish the tail as an
    annotation and in the run summary, and still exit non-zero.
    """
    text = _text()
    assert re.search(r"wrangler@4 deploy\s*>\S+ 2>&1 \|\| status=\$\?", text), (
        "wrangler output is not captured, so its error text cannot be reported")
    assert '"::error::wrangler deploy failed' in text, (
        "the deploy failure reason is not published as an annotation")
    assert 'exit "$status"' in text, "wrangler failures must still fail the job"


def test_deploy_step_preserves_wrangler_exit_status():
    # `bash -e` would abort the step before anything is reported.
    text = _text()
    assert "status=0" in text and "|| status=$?" in text
