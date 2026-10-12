#!/usr/bin/sh
# fake pi: emits a minimal pi JSONL stream and exits 0
echo '{"type":"session_header","id":"fake","version":"x"}'
echo '{"type":"message_start","message":{"role":"assistant","content":[],"stopReason":"pending"}}'
echo '{"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"let me think hard"},{"type":"text","text":"hello from fake pi"}],"stopReason":"stop"}}'
echo '{"type":"agent_settled","aborted":false}'
exit 0
