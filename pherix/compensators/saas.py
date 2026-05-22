"""SaaS-API compensators — the everyday agent tool calls.

These are the inverses an agent wiring up GitHub / Slack / Stripe /
SendGrid / Twilio / Jira reaches for first:

  GitHub   create_pr      → close_pr            (open a PR → close it)
  GitHub   add_label      → remove_label        (label an issue → unlabel)
  Slack    post_message   → delete_message      (post → delete)
  Stripe   create_customer→ delete_customer     (create → delete)
  SendGrid add_contact    → remove_contact      (add to list → remove)
  Twilio   send_sms       → (GATE, no comp)     (cannot unsend an SMS)
  Jira     create_issue   → delete_issue        (create → delete)

Each clean pair reverses off a shared id in the args. ``send_sms`` has no
honest inverse (a delivered SMS is gone), so it gates exactly like
``send_email`` — the catalog stays honest about what it cannot undo.
"""

from __future__ import annotations

from pherix.core.tools import tool


# --- GitHub ---------------------------------------------------------------


def register_github_pr(client, *, resource: str = "github"):
    """Register ``create_pr`` and its left-inverse ``close_pr``.

    ``client`` must expose::

        client.create_pr(repo, branch, title, body) -> object
        client.close_pr(repo, branch) -> object

    Reverses by ``(repo, branch)`` — closing the PR opened from that branch.
    Closing (not merging) is the honest inverse of opening: an open PR has
    not yet changed the base branch, so closing it returns the repo to its
    pre-PR state.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def close_pr(repo, branch, title, body):
        return client.close_pr(repo, branch)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="close_pr",
    )
    def create_pr(repo, branch, title, body):
        return client.create_pr(repo, branch, title, body)

    return create_pr, close_pr


def register_github_label(client, *, resource: str = "github"):
    """Register ``add_label`` and its left-inverse ``remove_label``.

    ``client`` must expose::

        client.add_label(repo, issue, label) -> object
        client.remove_label(repo, issue, label) -> object

    Reverses by ``(repo, issue, label)``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def remove_label(repo, issue, label):
        return client.remove_label(repo, issue, label)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="remove_label",
    )
    def add_label(repo, issue, label):
        return client.add_label(repo, issue, label)

    return add_label, remove_label


# --- Slack ----------------------------------------------------------------


def register_slack_message(client, *, resource: str = "slack"):
    """Register ``post_message`` and its left-inverse ``delete_message``.

    ``client`` must expose::

        client.post_message(channel, ts, text) -> object
        client.delete_message(channel, ts) -> object

    Reverses by ``(channel, ts)``. The caller supplies the message timestamp
    ``ts`` as the idempotency key, so the action and its deletion share the
    handle the compensator receives — Slack's own ``chat.delete`` keys on
    exactly ``(channel, ts)``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def delete_message(channel, ts, text):
        return client.delete_message(channel, ts)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="delete_message",
    )
    def post_message(channel, ts, text):
        return client.post_message(channel, ts, text)

    return post_message, delete_message


# --- Stripe ---------------------------------------------------------------


def register_stripe_customer(client, *, resource: str = "stripe"):
    """Register ``create_customer`` and its left-inverse ``delete_customer``.

    ``client`` must expose::

        client.create_customer(customer_id, email) -> object
        client.delete_customer(customer_id) -> object

    Reverses by ``customer_id``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def delete_customer(customer_id, email):
        return client.delete_customer(customer_id)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="delete_customer",
    )
    def create_customer(customer_id, email):
        return client.create_customer(customer_id, email)

    return create_customer, delete_customer


# --- SendGrid -------------------------------------------------------------


def register_sendgrid_contact(client, *, resource: str = "sendgrid"):
    """Register ``add_contact`` and its left-inverse ``remove_contact``.

    ``client`` must expose::

        client.add_contact(list_id, email) -> object
        client.remove_contact(list_id, email) -> object

    Reverses by ``(list_id, email)``. Adding a contact to a marketing list
    is cleanly reversible — note this is distinct from *sending* mail to
    them, which is not (see ``identity.register_send_email_gate``).
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def remove_contact(list_id, email):
        return client.remove_contact(list_id, email)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="remove_contact",
    )
    def add_contact(list_id, email):
        return client.add_contact(list_id, email)

    return add_contact, remove_contact


# --- Twilio ---------------------------------------------------------------


def register_twilio_sms_gate(client, *, resource: str = "twilio"):
    """Register ``send_sms`` with **no compensator** — it gates at commit.

    ``client`` must expose ``client.send_sms(to, body) -> object``.

    Like a delivered email, a delivered SMS has no honest inverse, so the
    action stages and ``commit()`` blocks until ``approve_irreversible()``.
    Returns the action only.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def send_sms(to, body):
        return client.send_sms(to, body)

    return send_sms


# --- Jira -----------------------------------------------------------------


def register_jira_create_delete_issue(client, *, resource: str = "jira"):
    """Register ``create_issue`` and its left-inverse ``delete_issue``.

    ``client`` must expose::

        client.create_issue(issue_key, project, summary) -> object
        client.delete_issue(issue_key) -> object

    Reverses by ``issue_key``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def delete_issue(issue_key, project, summary):
        return client.delete_issue(issue_key)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="delete_issue",
    )
    def create_issue(issue_key, project, summary):
        return client.create_issue(issue_key, project, summary)

    return create_issue, delete_issue
