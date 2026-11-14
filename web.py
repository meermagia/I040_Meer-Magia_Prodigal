"""Optional web chat UI over the SAME agent core as the CLI (agent.start_session / run_turn /
finish_session). No separate LLM loop, no canned replies.

    streamlit run web.py
"""
import html
import json

import streamlit as st

import agent
from api import APIError
from validation import COLLECTABLE, record_issues

st.set_page_config(page_title="ACME · Application Assistant", page_icon="💬", layout="wide")

st.markdown("""
<style>
.chip{display:inline-block;padding:2px 10px;margin:2px 4px 2px 0;border-radius:999px;font-size:12px;
      border:1px solid transparent}
.chip.green{background:rgba(46,160,67,.15);color:#56d364;border-color:rgba(46,160,67,.4)}
.chip.amber{background:rgba(210,153,34,.15);color:#e3b341;border-color:rgba(210,153,34,.4)}
.chip.red{background:rgba(248,81,73,.15);color:#ff7b72;border-color:rgba(248,81,73,.4)}
.chip.blue{background:rgba(56,139,253,.15);color:#79c0ff;border-color:rgba(56,139,253,.4)}
.chip.grey{background:rgba(139,148,158,.15);color:#b1bac4;border-color:rgba(139,148,158,.4)}
.badge{display:inline-block;padding:4px 14px;border-radius:999px;font-weight:600;font-size:14px}
[data-testid="stChatMessage"]{border-radius:16px}
</style>""", unsafe_allow_html=True)

STATUS_COLOR = {"completed": "green", "in_progress": "blue", "refused": "amber",
                "no_answer": "grey", "needs_human": "red"}
TRACKED = ["full_name"] + COLLECTABLE  # phone is read-only and excluded

ss = st.session_state
for k, v in {"conv": None, "chat": [], "ended": False, "error": None, "candidates": None}.items():
    ss.setdefault(k, v)


# ------------------------------------------------------------ helpers
def chips_for(event):
    """Coloured pills for one tool call."""
    name, r = event["name"], event["result"]
    if "error" in r:
        return [("red", f"{name} → error: {r['error'][:80]}")]
    if name == "confirm_identity":
        return [("blue", "confirm_identity ✓")] if r.get("identity") else [("red", "confirm_identity → not the candidate")]
    if name == "get_candidate":
        return [("blue", "get_candidate")]
    if name == "end_conversation":
        return [(STATUS_COLOR.get(r["status"], "grey"), f"end_conversation → {r['status']}")]
    out = []
    if name == "save_fields":
        if r.get("saved"):
            out.append(("green", "save_fields → saved: " + ", ".join(r["saved"])))
        for f, why in (r.get("rejected") or {}).items():
            out.append(("amber", f"rejected: {f} ({why[:60]})"))
        for f in (r.get("not_saved_existing_value") or {}):
            out.append(("amber", f"{f}: needs candidate confirmation"))
        if r.get("conflict"):
            out.append(("amber", "409 conflict → re-read, kept server value: " + ", ".join(r["conflict"]["fields"])))
        if not out:
            out.append(("grey", "save_fields → no change"))
    return out


def render_events(events):
    if not events:
        return
    pills = "".join(f'<span class="chip {c}">{html.escape(t)}</span>' for e in events for c, t in chips_for(e))
    st.markdown(pills, unsafe_allow_html=True)
    with st.expander("Agent actions"):
        for e in events:
            try:
                args = json.loads(e["args"]) if isinstance(e["args"], str) else e["args"]
            except ValueError:
                args = e["args"]
            st.markdown(f"**{e['name']}**")
            st.json({"args": args, "result": e["result"]}, expanded=False)


def add_turn(result):
    for i, text in enumerate(result.texts or [""]):
        events = result.tool_events if i == len(result.texts or [""]) - 1 else []
        if text or events:
            ss.chat.append({"role": "assistant", "text": text, "tool_events": events})
    if result.error:
        ss.error = result.error
    if result.ended or ss.conv.closed:
        close_conv("conversation ended")


def close_conv(reason, left=False):
    conv = ss.conv
    if conv and conv.session and not conv.closed:
        res = agent.finish_session(conv, reason, left=left)
        if res is not None and (res.texts or res.tool_events):
            ss.chat.append({"role": "assistant", "text": " ".join(res.texts), "tool_events": res.tool_events})
    ss.ended = True


def start(cid):
    if ss.conv and ss.conv.session and not ss.conv.closed:
        close_conv("candidate left (operator started a new session)", left=True)
    ss.update(conv=None, chat=[], ended=False, error=None)
    try:
        agent.load_env()
        with st.spinner("Fetching candidate and opening chat…"):
            conv = agent.start_session(cid, interface="web")
    except (APIError, RuntimeError) as e:
        ss.error = {404: f"No candidate with id {cid}."}.get(getattr(e, "status", None), f"Could not start: {e}")
        return
    ss.conv = conv
    if conv.no_chat:
        ss.ended = True
        return
    add_turn(conv.first)


# ------------------------------------------------------------ sidebar (operator)
with st.sidebar:
    st.header("Operator view")
    st.caption("For the person running the demo. The candidate types in the chat.")
    if ss.candidates is None:
        try:
            agent.load_env()
            api = agent.CandidateAPI(agent.os.environ["CANDIDATE_API_BASE_URL"], agent.os.environ["CANDIDATE_API_KEY"])
            ss.candidates = api._request("GET", "/v1/candidates").get("candidates", [])
        except Exception as e:  # noqa: BLE001 - shown to the operator only
            ss.candidates = []
            st.warning(f"Could not list candidates: {e}")
    options = [f"{c['id']} · {c['full_name']} · {c['collection_status']}" for c in ss.candidates]
    pick = st.selectbox("Candidate", options) if options else None
    typed = st.text_input("…or type a candidate ID", placeholder="e.g. c004")
    if st.button("Start conversation", type="primary", use_container_width=True):
        cid = typed.strip() or (pick.split(" · ")[0] if pick else "")
        if cid:
            start(cid)
            st.rerun()

    conv = ss.conv
    if conv and conv.record:
        rec = conv.session.record if conv.session else conv.record
        confirmed = bool(conv.session and conv.session.identity is True)
        st.divider()
        st.subheader("Mandatory fields")
        missing, invalid = record_issues(rec)
        valid = [f for f in TRACKED if f not in missing and f not in invalid]
        st.progress(len(valid) / len(TRACKED), text=f"{len(valid)}/{len(TRACKED)} valid")
        for f in TRACKED:
            if f in missing:
                st.markdown(f"✗ `{f}` · missing")
            elif f in invalid:
                reason = invalid[f][:60]
                shown = f" = {html.escape(str(rec.get(f)))}" if confirmed or not conv.session else ""
                st.markdown(f"⚠ `{f}`{shown} · {reason}")
            else:
                shown = f" = {rec.get(f)}" if confirmed or not conv.session else ""
                st.markdown(f"✓ `{f}`{shown}")
        if conv.session and not confirmed:
            st.caption("Values hidden until the candidate confirms their identity.")
        st.divider()
        c1, c2, c3 = st.columns(3)
        c1.metric("Status", rec.get("collection_status") or "-")
        c2.metric("Version", rec.get("version"))
        c3.metric("Attempts", rec.get("attempt_count"))
        if conv.session and not ss.ended:
            if st.button("Candidate leaves / End chat", type="secondary", use_container_width=True):
                with st.spinner("Closing the conversation…"):
                    close_conv("candidate left the chat", left=True)
                st.rerun()
    st.caption("Silence timeout is CLI-only; in the web UI use 'Candidate leaves'.")


# ------------------------------------------------------------ main chat
st.title("ACME Corp · Internship Application Assistant")
st.caption("AI agent completing candidate applications over chat")

conv = ss.conv
if conv is None and not ss.error:
    st.info("Pick a candidate in the sidebar and press **Start conversation**.")

if conv and conv.no_chat:
    st.success("Record already complete. No conversation started.")
    st.caption(conv.no_chat)

for m in ss.chat:
    avatar = "🤖" if m["role"] == "assistant" else "🧑‍🎓"
    with st.chat_message(m["role"], avatar=avatar):
        if m["text"]:
            st.markdown(m["text"])
        if m["role"] == "assistant":
            render_events(m.get("tool_events"))

if ss.error:
    st.error(ss.error)

if conv and conv.session and ss.ended:
    s = conv.session
    rec = s.record
    status = s.final_status or "not saved"
    color = STATUS_COLOR.get(status, "grey")
    rejected = sorted({f for m in ss.chat for e in (m.get("tool_events") or [])
                       if e["name"] == "save_fields" for f in (e["result"].get("rejected") or {})})
    with st.container(border=True):
        st.markdown(f'Outcome &nbsp; <span class="chip {color} badge">{status}</span>', unsafe_allow_html=True)
        a, b = st.columns(2)
        a.markdown(f"**Saved this session:** {', '.join(s.saved_fields) or '-'}")
        a.markdown(f"**Rejected attempts:** {', '.join(rejected) or '-'}")
        b.markdown(f"**attempt_count:** {rec.get('attempt_count')}")
        b.markdown(f"**last_contacted_at:** {rec.get('last_contacted_at')}")
        notes = (rec.get("notes") or "").splitlines()
        st.markdown("**Note appended:**")
        st.code(notes[-1] if notes else "-", language=None)
        st.caption(f"Run log: {conv.log.base.relative_to(agent.ROOT)}.jsonl")

if prompt := st.chat_input("Type your reply as the candidate…" if not ss.ended else "Conversation ended",
                           disabled=ss.ended or conv is None or conv.session is None):
    ss.chat.append({"role": "user", "text": prompt, "tool_events": []})
    with st.chat_message("user", avatar="🧑‍🎓"):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar="🤖"):
        with st.spinner("Assistant is typing…"):
            result = agent.run_turn(conv, prompt)
    add_turn(result)
    st.rerun()
