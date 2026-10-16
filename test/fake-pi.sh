#!/usr/bin/sh
# fake pi: emits a minimal pi JSONL stream and exits 0
# (thinking is streamed like real pi: message_update thinking_start/
# delta/end, then repeated authoritatively in message_end)
echo '{"type":"session_header","id":"fake","version":"x"}'
echo '{"type":"message_start","message":{"role":"assistant","content":[],"stopReason":"pending"}}'
echo '{"type":"message_update","assistantMessageEvent":{"type":"thinking_start","contentIndex":0}}'
echo '{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","contentIndex":0,"delta":"let me "}}'
echo '{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","contentIndex":0,"delta":"think hard"}}'
echo '{"type":"message_update","assistantMessageEvent":{"type":"thinking_end","contentIndex":0,"content":"let me think hard"}}'
echo '{"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"let me think hard"},{"type":"text","text":"hello from fake pi"}],"stopReason":"stop"}}'
echo '{"type":"agent_settled","aborted":false}'
exit 0
