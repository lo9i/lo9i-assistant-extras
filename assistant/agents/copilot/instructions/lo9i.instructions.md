---
applyTo: "**"
---
lo9i, the user's assistant, started this session, and its daemon waits for your reply. Never stop or kill the daemon's process (`assistant.daemon`, `stop_daemon` in lo9i's scripts): that ends this session before you reply, and any daemon you start from it. To restart it, for example so lo9i code changes take effect, run `curl -s -X POST --unix-socket ~/.lo9i/daemon.sock -H 'X-Lo9i-Client: copilot' http://lo9i/system/restart`: it restarts once your reply is in, so tell the user the change applies from their next message.
