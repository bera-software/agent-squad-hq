#!/usr/bin/env python3
"""
Debt re-proposal guard, used by tech-debt-weekly.yml's "Deliver findings" step.

The scanner only de-duplicated against OPEN debt issues, so a finding the team
had already settled came back as a fresh proposal a few days later (e.g. a
class split an ADR had already rejected). A closed issue's state_reason does
not say whether it was fixed or declined (an issue closed "completed" may
record a decision NOT to act), so the guard matches on WHAT the
finding is about — its anchors: file paths and CamelCase class names in the
title and first lines — against debt issues closed within the window. Any
match suppresses the new proposal, except severity:high (security/CVE), which
always goes through. Suppressed findings are listed in the run summary with
the prior issue, so nothing disappears silently.
"""
import re

from squadhq import untrusted

PATH_RE = re.compile(r"[\w./-]+/[\w.-]+\.(?:java|kt|ts|tsx|js|jsx|py|go|rb|cs|sql|yml|yaml)\b")
CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){1,}\b")
WINDOW_DAYS = 90


def anchors(text):
    text = text or ""
    paths = {p.rsplit("/", 1)[-1] for p in PATH_RE.findall(text)}   # file name, path-independent
    names = set(CAMEL_RE.findall(text))
    return {a.lower() for a in paths | names}


def prior_match(finding, closed_issues):
    """-> the closed issue this finding repeats, or None."""
    if "severity:high" in (finding.get("labels") or []):
        return None
    head = (finding.get("title") or "") + "\n" + "\n".join((finding.get("body") or "").splitlines()[:5])
    mine = anchors(head)
    if not mine:
        return None
    for it in closed_issues:
        if not _settled_by_us(it):
            continue
        theirs = anchors((it.get("title") or "") + "\n" + "\n".join((it.get("body") or "").splitlines()[:5]))
        if mine & theirs:
            return it
    return None


def _settled_by_us(issue):
    """A closed issue only counts as a settled decision when our bot or a
    trusted association opened it: anyone can open-and-close an issue named
    after a real finding to suppress it. Callers should request `author`;
    without author info the debt/debt-proposal label filter (which needs
    triage rights to apply) is the only evidence, and is accepted."""
    if "author" not in issue and "user" not in issue:
        return True
    # Debt issues are opened by the debt-scanner agent: ops, agent or a trusted human.
    return untrusted.by_automation_or_trusted(issue)
