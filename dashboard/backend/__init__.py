"""Pherix control plane — the commercial multi-tenant service layer.

Greenfield service that *consumes* the single-host substrate (the audit journal,
the inspector, the envelope) without ever reaching into the engine. It holds the
org-scale metadata — orgs/users/keys, the agent registry, versioned policy
definitions, and the *retained* journal — that a fleet of agents on many hosts
needs, while execution and the user's resources stay entirely on the data plane.

Nothing here is a dependency of the OSS SDK. The SDK keeps working fully offline;
journal-ship is opt-in and redacted client-side before it leaves the agent's host.
"""
