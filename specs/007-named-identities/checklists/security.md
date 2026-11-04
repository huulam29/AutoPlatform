# Security Checklist: Named Identities

**Purpose**: Requirements quality for auth, secrets and audit (Lane 2 checklist)
**Created**: 2026-11-03
**Feature**: [spec.md](../spec.md)

- [x] CHK001 Is it specified where tokens are stored? (FR-001: nowhere; only hashes, FR-002)
- [x] CHK002 Is the one place a token may appear in a response specified? (FR-006, contracts)
- [x] CHK003 Are listing responses specified to exclude hashes? (FR-007)
- [x] CHK004 Is constant-time comparison required? (FR-005)
- [x] CHK005 Is revocation latency specified and measurable? (FR-005, SC-002)
- [x] CHK006 Is it specified who may mint tokens, including the shared token? (FR-006)
- [x] CHK007 Is lock-out prevention specified? (US4 #3: local user always admin)
- [x] CHK008 Are reserved names that could spoof another identity kind refused? (FR-013)
- [x] CHK009 Is the Chat kept away from token values? (FR-012)
- [x] CHK010 Are people-management changes audited without the token? (FR-011)
- [x] CHK011 Is behaviour with no people configured specified as unchanged? (FR-014)
- [x] CHK012 Is a corrupt people file's behaviour specified? (Edge cases)
- [x] CHK013 Is a bearer that matches nobody on an open loopback engine specified? (plan: 401)
- [x] CHK014 Are out-of-scope powers (roles, chat links) stated? (Assumptions)
