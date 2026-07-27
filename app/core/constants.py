"""Shared constants that multiple core modules import.

Keep this module dependency-free (stdlib only) so that importing a
constant never drags in a view's implementation or its third-party
dependencies.
"""

# Session key for the Slack team name captured at login, used to prefill
# the workspace name during onboarding.
SLACK_TEAM_NAME_SESSION_KEY = "slack_team_name"

# Slack's OpenID Connect userInfo response namespaces its custom claims.
SLACK_TEAM_NAME_CLAIM = "https://slack.com/team_name"
