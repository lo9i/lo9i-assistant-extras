"""What lo9i's chat channel plugins (Telegram, Slack) share: a client for the daemon's channel routes
(client.py), the runner that keeps its stream open (runner.py), run events (events.py), and how a
run is shown as chat messages (turn.py), with approval and question buttons and long replies split.
Each channel implements `ChatPort` for its chat service and `Channel` for the daemon's stream."""
