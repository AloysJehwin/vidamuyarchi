#!/usr/bin/env python3
"""Generate the n8n workflow JSON files for the recruiter outreach pipeline (n8n 2.31.x).
People come from the LinkedIn DB and the Names sheet; the Python engine verifies addresses without paid APIs."""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent / "workflows"
MAIN_ID, ERR_ID, FU_ID = "RcOutreachMain01", "RcOutreachErr001", "RcOutreachFwup01"
INTAKE_ID, BOUNCE_ID = "RcOutreachIntk01", "RcOutreachBnce01"
# Personal values come from config/private/n8n.json (gitignored): {"approver", "sheet_id", "outreach_dir"}.
_PRIVATE_JSON = Path(__file__).resolve().parents[2] / "config" / "private" / "n8n.json"
_PRIV = json.loads(_PRIVATE_JSON.read_text()) if _PRIVATE_JSON.exists() else {}
APPROVER = _PRIV.get("approver", "you@example.com")
SHEET_ID = _PRIV.get("sheet_id", "")
OUTREACH_DIR = _PRIV.get("outreach_dir", "/home/YOUR_USER/outreach")
SHEET_TAB = "Untitled"  # the Names sheet's only tab (referenced by name, not gid)
BOUNCE_QUERY = "from:(mailer-daemon OR postmaster) newer_than:2d"
GMAIL_CRED = {"gmailOAuth2": {"id": "KXJzqnGqly6c8ZyZ", "name": "Gmail account"}}
SHEETS_CRED = {"googleSheetsOAuth2Api": {"id": "ggoogle76518", "name": "Google Sheets OSS"}}
CFG = "$('Config').first().json"
PY = f"cd {{{{ {CFG}.outreach_dir }}}} && {{{{ {CFG}.python }}}} pipeline.py"
RX_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
RX_PID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$"
RX_EMAIL = r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"
DELAY_RX = r"\(delay\)|delivery incomplete|will (?:retry|keep trying)|has been delayed|delivery delayed"  # = sending.DELAY_RE
ALERT_DUE = "$execution.mode === 'test' || ($now.hour === 19 && $now.minute < 20)"

PARSE_JS = """const out = $input.first().json;
const lines = String(out.stdout || '').trim().split('\\n').filter(l => l.trim());
let data = null;
try { data = JSON.parse(lines[lines.length - 1]); } catch (e) { data = null; }
if (!data || typeof data !== 'object') {
  throw new Error(`pipeline.py returned no JSON (exit ${out.exitCode}). stderr: ${String(out.stderr || '').slice(-1200)} stdout: ${String(out.stdout || '').slice(-300)}`);
}
if (out.exitCode !== 0 && data.ok !== false) throw new Error(`pipeline.py exit ${out.exitCode}: ${String(out.stderr || '').slice(-1200)}`);
return [{ json: data, pairedItem: { item: 0 } }];"""

GAP_JS = """const cfg = $('Config').first().json;
const lo = Number(cfg.min_gap_seconds) || 30, hi = Math.max(Number(cfg.max_gap_seconds) || 60, lo);
return [{ json: { ...$input.first().json, gap: Math.floor(lo + Math.random() * (hi - lo + 1)) }, pairedItem: { item: 0 } }];"""

CHECK_CONFIG_JS = """const c = $('Config').first().json, bad = [], email = /""" + RX_EMAIL + """/;
for (const k of ['outreach_dir', 'python']) if (!/^\\/[A-Za-z0-9._\\/-]+$/.test(String(c[k] || ''))) bad.push(`${k} must be an absolute path`);
for (const [k, lo, hi] of [['limit', 0, 500], ['intake_time_budget_s', 60, 500], ['intake_probe_budget', 0, 1000]])
  if (k in c && !(Number.isInteger(c[k]) && c[k] >= lo && c[k] <= hi)) bad.push(`${k} must be a whole number from ${lo} to ${hi}`);
if (c.tier && !/^[ABC]$/.test(String(c.tier))) bad.push('tier must be A, B, C or empty');
if (c.test_recipient && !email.test(String(c.test_recipient))) bad.push('test_recipient must be one plain email address');
if (bad.length) throw new Error('Config: ' + bad.join('; '));
return $input.all();"""
ROWS_TO_CSV_JS = """const cols = ['name', 'first_name', 'last_name', 'company', 'job_title', 'city', 'country', 'domain', 'tier', 'email_hash', 'linkedin_url'];
const alias = { full_name: 'name', title: 'job_title', profile_url: 'linkedin_url', linkedin: 'linkedin_url' };
const esc = v => { const s = String(v ?? '').replace(/[\\r\\n]+/g, ' ').trim(); return /[",]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
const rows = $input.all().map(it => Object.entries(it.json || {}).reduce((r, [k, v]) => {
  const key = String(k).trim().toLowerCase().replace(/\\s+/g, '_'), c = alias[key] || key;
  if (!r[c]) r[c] = String(v ?? '').trim();
  return r;
}, {})).filter(r => r.name && r.company && !r.status).map(r => ({ ...r,
  domain: String(r.domain || '').toLowerCase().replace(/^[a-z]+:\\/\\//, '').replace(/^www\\./, '').split(/[\\/?#]/)[0] }));
const csv = [cols.join(',')].concat(rows.map(r => cols.map(c => esc(r[c])).join(','))).join('\\n') + '\\n';
return [{ json: { csv, rows: rows.length, file_name: 'sheet.csv' } }];"""
SHEET_ROWS_JS = """const cols = ['name', 'company', 'status', 'email', 'confidence', 'method', 'note', 'updated_at'];
return ($('Parse sheet-status').first().json.rows || [])
  .filter(r => String(r.source || '').toLowerCase() !== 'linkedin' && !String(r.prospect_id || '').startsWith('li-'))
  .map(r => ({ json: Object.fromEntries(cols.filter(c => c in r).map(c => [c, r[c]])) }));"""
BOUNCE_EXTRACT_JS = """const me = String($('Config').first().json.sender_address || '').toLowerCase(), id = /""" + RX_ID + """/, email = /""" + RX_EMAIL + """/, seen = new Set(), out = [];
const pick = list => list.map(s => String(s).trim().toLowerCase().replace(/^<|>$/g, ''))
  .find(s => email.test(s) && s !== me && !/^(mailer-daemon|postmaster)@/.test(s)) || '';
$input.all().forEach((it, i) => {
  const m = it.json || {}, h = Object.fromEntries(Object.entries(m).map(([k, v]) => [k.toLowerCase(), v]));
  const subject = String(h.subject || ''), snippet = String(m.snippet || ''), thread = String(m.threadId || '');
  if (/""" + DELAY_RX + """/i.test(subject + ' ' + snippet) || !id.test(thread)) return;
  const to = pick(String(h['x-failed-recipients'] || '').split(/[,;\\s]+/)) || pick(snippet.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\\.[A-Za-z0-9-]+)+/g) || []);
  if (seen.has(thread + '|' + to)) return; else seen.add(thread + '|' + to);
  out.push({ json: { thread_id: thread, email: to, subject: subject.slice(0, 160), snippet: snippet.slice(0, 300) }, pairedItem: { item: i } });
});
return out;"""
BOUNCE_PARSE_JS = """let res = null;
try { res = JSON.parse(String($json.stdout || '').trim().split('\\n').filter(l => l.trim()).pop()); } catch (e) { res = null; }
if (!res || typeof res !== 'object') throw new Error(`pipeline.py bounce returned no JSON (exit ${$json.exitCode}). stderr: ${String($json.stderr || '').slice(-600)}`);
const hit = res.ok !== false && res.matched ? String(res.matched) : '', note = res.ok === false ? String(res.error || res.reason || 'ok=false').slice(0, 200) : '';
"""
BY_THREAD_JS = BOUNCE_PARSE_JS + "return { json: { ...$('Extract bounce keys').item.json, matched: hit, via: hit ? 'thread' : '', note } };"
BY_EMAIL_JS = BOUNCE_PARSE_JS + """const prev = $('Parse thread result').item.json;
return { json: { ...prev, matched: hit || prev.matched, via: hit ? 'email' : prev.via, note: [prev.note, note].filter(Boolean).join('; ') } };"""
BOUNCE_REPORT_JS = """const rows = $input.all().map(i => i.json), hit = rows.filter(r => r.matched), miss = rows.filter(r => !r.matched);
const lines = hit.map(r => `BOUNCED   ${r.matched}  ${r.email || '(address not parsed)'}  (matched by ${r.via})`)
  .concat(miss.map(r => `no match  ${r.email || '(address not parsed)'}  ${r.subject}${r.note ? '  [' + r.note + ']' : ''}`));
return [{ json: { found: rows.length, matched: hit.length, unmatched: miss.length, text: lines.join('\\n') } }];"""


class WF:
    def __init__(self, name, wf_id, error_workflow=None):
        self.name, self.id, self.nodes, self.conn = name, wf_id, [], {}
        self.settings = {"executionOrder": "v1", "timezone": "Asia/Kolkata", "executionTimeout": 590,
                         "saveManualExecutions": True, "saveExecutionProgress": True, "saveDataErrorExecution": "all",
                         "saveDataSuccessExecution": "all", "callerPolicy": "workflowsFromSameOwner"}
        if error_workflow:
            self.settings["errorWorkflow"] = error_workflow

    def add(self, name, ntype, version, params, pos, **extra):
        node = {"id": str(uuid.uuid4()), "name": name, "type": ntype, "typeVersion": version,
                "position": [pos[0] * 260, pos[1] * 200], "parameters": params}
        node.update(extra)
        self.nodes.append(node)
        return name

    def link(self, src, dst, out=0):
        self.conn.setdefault(src, {"main": []})
        while len(self.conn[src]["main"]) <= out:
            self.conn[src]["main"].append([])
        self.conn[src]["main"][out].append({"node": dst, "type": "main", "index": 0})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def note(self, content, pos, w=520, h=300, color=4):
        self.nodes.append({"id": str(uuid.uuid4()), "name": f"Note {len(self.nodes)}", "type": "n8n-nodes-base.stickyNote",
                           "typeVersion": 1, "position": [pos[0] * 260, pos[1] * 200],
                           "parameters": {"content": content, "width": w, "height": h, "color": color}})

    def dump(self, path):
        names = [n["name"] for n in self.nodes]
        assert len(names) == len(set(names)), "duplicate node names"
        assert self.settings.get("executionTimeout", 0) <= 590, "EXECUTIONS_TIMEOUT_MAX on the Razer is 600"
        for src, c in self.conn.items():
            assert src in names, src
            for branch in c["main"]:
                for t in branch:
                    assert t["node"] in names, t["node"]
        data = {"id": self.id, "name": self.name, "nodes": self.nodes, "connections": self.conn,
                "settings": self.settings, "staticData": None, "pinData": {}, "tags": [], "active": False,
                "versionId": str(uuid.uuid4())}
        path.write_text(json.dumps(data, indent=2))
        return path


def safe(expr, rx=RX_ID, fallback="invalid-id"):
    return f"{{{{ (/{rx}/.test(String({expr} || '')) ? String({expr}) : '{fallback}') }}}}"


def exec_node(wf, name, cmd_expr, pos, once=True, **extra):
    return wf.add(name, "n8n-nodes-base.executeCommand", 1,
                  {"command": f"={PY} {cmd_expr}", "executeOnce": once}, pos, **extra)


def parse_node(wf, name, pos):
    return wf.add(name, "n8n-nodes-base.code", 2, {"jsCode": PARSE_JS}, pos)


def code_node(wf, name, js, pos, per_item=False):
    return wf.add(name, "n8n-nodes-base.code", 2, {"mode": "runOnceForEachItem", "jsCode": js} if per_item else {"jsCode": js}, pos)


def if_bool(wf, name, expr, pos):
    return wf.add(name, "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": f"={{{{ {expr} }}}}", "rightValue": "",
                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos)


def gmail_send(wf, name, subject_expr, message_expr, pos, to_expr=None, html=False, **extra):
    return wf.add(name, "n8n-nodes-base.gmail", 2.1, {
        "sendTo": to_expr or f"={{{{ {CFG}.approver_email }}}}", "subject": subject_expr,
        "emailType": "html" if html else "text",
        "message": message_expr, "options": {"appendAttribution": False}}, pos, credentials=GMAIL_CRED, **extra)


def gmail_approval(wf, name, subject_expr, message_expr, approve_label, pos):
    return wf.add(name, "n8n-nodes-base.gmail", 2.1, {
        "resource": "message", "operation": "sendAndWait", "sendTo": f"={{{{ {CFG}.approver_email }}}}",
        "subject": subject_expr, "message": message_expr, "responseType": "approval",
        "approvalOptions": {"values": {"approvalType": "double", "approveLabel": approve_label,
                                       "buttonApprovalStyle": "primary", "disapproveLabel": "Cancel - send nothing",
                                       "buttonDisapprovalStyle": "secondary"}},
        "options": {"appendAttribution": False,
                    "limitWaitTime": {"values": {"limitType": "afterTimeInterval",
                                                 "resumeAmount": f"={{{{ {CFG}.approval_wait_hours }}}}",
                                                 "resumeUnit": "hours"}}}}, pos, credentials=GMAIL_CRED)


def gmail_raw_send(wf, name, pos, with_thread=False):
    body = "={{ JSON.stringify({ raw: $json.raw, threadId: $json.thread_id || undefined }) }}" if with_thread \
        else "={{ JSON.stringify({ raw: $json.raw }) }}"
    return wf.add(name, "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "gmailOAuth2",
        "sendBody": True, "specifyBody": "json", "jsonBody": body,
        "options": {"timeout": 60000}}, pos, credentials=GMAIL_CRED, onError="continueErrorOutput", retryOnFail=False)


def sheets_node(wf, name, params, pos, **extra):
    base = {"documentId": {"__rl": True, "value": f"={{{{ {CFG}.sheet_id }}}}", "mode": "id"},
            "sheetName": {"__rl": True, "value": f"={{{{ {CFG}.sheet_tab }}}}", "mode": "name"}}
    return wf.add(name, "n8n-nodes-base.googleSheets", 4.5, {**base, **params}, pos, credentials=SHEETS_CRED, **extra)


def stop(wf, name, msg_expr, pos):
    return wf.add(name, "n8n-nodes-base.stopAndError", 1, {"errorMessage": msg_expr}, pos)


def triggers(wf, schedule_name, cron, pos):
    wf.add("Manual Trigger", "n8n-nodes-base.manualTrigger", 1, {}, pos)
    wf.add(schedule_name, "n8n-nodes-base.scheduleTrigger", 1.2,
           {"rule": {"interval": [{"field": "cronExpression", "expression": cron}]}}, (pos[0], pos[1] + 1))
    wf.link("Manual Trigger", "Config"); wf.link(schedule_name, "Config")


BASE_CFG = [("outreach_dir", OUTREACH_DIR, "string"), ("python", "/usr/bin/python3", "string")]
MAIL_CFG = [("approval_wait_hours", 24, "number"), ("approver_email", APPROVER, "string"), ("sender_address", APPROVER, "string"),
            ("min_gap_seconds", 30, "number"), ("max_gap_seconds", 60, "number"), ("test_recipient", "", "string")]
SHEET_CFG = [("sheet_id", SHEET_ID, "string"), ("sheet_tab", SHEET_TAB, "string")]


def config_node(wf, pos, fields):
    wf.add("Config", "n8n-nodes-base.set", 3.4, {
        "assignments": {"assignments": [{"id": str(uuid.uuid4()), "name": n, "value": v, "type": t} for n, v, t in fields]},
        "includeOtherFields": False, "options": {}}, pos)
    wf.link("Config", code_node(wf, "Check config", CHECK_CONFIG_JS, (pos[0] + 1, pos[1])))
    return "Check config"


SANITIZE = ".replace(/[^A-Za-z0-9 ._:%()\\/-]/g, ' ').slice(0, 400)"
ERROR_ARG = f"--error \"{{{{ String(($json.error && ($json.error.message || $json.error.description)) || JSON.stringify($json.error || $json)){SANITIZE} }}}}\""
TIER = f"{{{{ {CFG}.tier ? ' --tier ' + {CFG}.tier : '' }}}}"
TEST = f"{{{{ {CFG}.test_recipient ? ' --test' : '' }}}}"


def build_main():
    wf = WF("Recruiter Outreach - Find & Send", MAIN_ID, ERR_ID)
    # No execution timeout: n8n 2.x counts the time a run spends waiting (approval, gaps between sends) from
    # its original start, so a 590 s cap cancelled every batch approved after ~10 min, at the first gap.
    # The run stays bounded by approval_wait_hours and the batch size.
    wf.settings["executionTimeout"] = -1
    wf.note("## Recruiter Outreach - Find & Send (daily 09:30 IST)\n"
            "Sends from the verified backlog that **Intake & Verify** builds every evening (LinkedIn DB + the `Recruiter Outreach - Names` sheet). "
            "Nothing is imported or verified here.\n\n**Flow**: preflight (bounce breaker, send window from `OUTREACH_SEND_DAYS` = every day by "
            "default, backlog advisory) -> compose up to `limit` 50 (India first; per-company daily/monthly caps from `config/companies.csv`; never "
            "an excluded company or an address already emailed) -> one approval email with the full copy -> per recipient: atomic claim (re-checks the caps) -> "
            "Gmail API send -> record -> random 30-60 s gap -> summary email -> sheet write-back.\n\n**Failsafes**: engine cap of 50 first "
            "touches a day (follow-ups have their own 25); only verified addresses unless `OUTREACH_UNVERIFIED_PATTERN_SENDS` is on; no send "
            "retry; approval timeout or Cancel = nothing sent; Error Handler emails you on any crash; `test_recipient` routes every mail to you.\n\n"
            "**Sheet write-back**: `sheet-status` rows only (the engine leaves out LinkedIn-sourced people). appendOrUpdate matches on `name` "
            "(n8n uses only the first matching column); the sheet has no unique key column yet, so matching is unchanged.\n\nImported inactive with the 09:30 schedule enabled: run it once by hand with `test_recipient` set, then activate it.",
            (0, 0), 760, 560)
    triggers(wf, "Schedule daily 09:30 IST", "30 9 * * *", (0, 4))
    check = config_node(wf, (1, 4), [("limit", 50, "number")] + MAIL_CFG + [("tier", "", "string"),
                                     ("require_send_window", True, "boolean")] + BASE_CFG + SHEET_CFG)

    exec_node(wf, "Preflight", "preflight", (3, 4))
    parse_node(wf, "Parse Preflight", (4, 4))
    if_bool(wf, "Preflight OK?", f"$json.ok === true && (!{CFG}.require_send_window || $json.send_window_ok === true)", (5, 4))
    gmail_send(wf, "Alert: preflight failed", "=[Outreach] Preflight failed - nothing sent",
               "=Preflight did not pass. Nothing was composed or sent.\n\nFailed checks: {{ ($json.failed || []).join(', ') || 'none' }}"
               "{{ !$json.send_window_ok ? '\\nOutside the send window (OUTREACH_SEND_DAYS; require_send_window is on).' : '' }}\n\n"
               "{{ JSON.stringify($json.checks, null, 2) }}", (6, 5))
    stop(wf, "Stop: preflight", "=Preflight failed: {{ ($('Parse Preflight').first().json.failed || []).join(', ') || 'send window' }}", (7, 5))
    wf.chain(check, "Preflight", "Parse Preflight", "Preflight OK?")
    wf.link("Preflight OK?", "Alert: preflight failed", 1); wf.link("Alert: preflight failed", "Stop: preflight")

    exec_node(wf, "Compose (MIME + PDF)",
              f"compose --limit {{{{ {CFG}.limit }}}}{TIER}{{{{ {CFG}.test_recipient ? ' --test-recipient ' + {CFG}.test_recipient : '' }}}}", (6, 4))
    parse_node(wf, "Parse Compose", (7, 4))
    if_bool(wf, "Anything composed?", "Number($json.count || 0) > 0", (8, 4))
    gmail_send(wf, "Notice: nothing verified", "=[Outreach] Nothing to send today (no verified addresses)",
               "=No verified address was ready for today's batch.{{ $json.ok === false ? ' Compose failed: ' + String($json.error || $json.reason || '') : '' }}"
               "\n\nSends today: {{ $json.sends_today }}   Room today: {{ $json.room_today }}   Held back: {{ $json.skipped_pending || 0 }}\n\nPreflight "
               "advisories:\n{{ ($('Parse Preflight').first().json.checks || []).filter(c => !c.ok).map(c => '- ' + c.name + ': ' + c.detail).join('\\n') || '- none' }}"
               "\n\nIntake & Verify refills the backlog every evening (19:00-23:40 IST). Unverified Names-sheet rows are written back with their best guess.",
               (9, 3))
    wf.add("Done: nothing", "n8n-nodes-base.noOp", 1, {}, (10, 3))
    wf.chain("Preflight OK?", "Compose (MIME + PDF)", "Parse Compose", "Anything composed?")
    wf.link("Anything composed?", "Notice: nothing verified", 1); wf.link("Notice: nothing verified", "Done: nothing")

    compose_run = safe("$('Parse Compose').first().json.run_id")
    gmail_approval(wf, "Approval: review full copy",
                   "={{ $json.test_mode ? '[TEST MODE] ' : '' }}[Outreach] Approve {{ $json.count }} recruiter emails - run {{ $json.run_id }}",
                   "={{ $json.digest }}", "=Send all {{ $('Parse Compose').first().json.count }}", (9, 4))
    if_bool(wf, "Approved?", "$json.data && $json.data.approved === true", (10, 4))
    exec_node(wf, "Abort run (declined)", f"abort --run-id {compose_run} --reason \"declined or approval timed out\"", (11, 5))
    gmail_send(wf, "Notice: batch cancelled", "=[Outreach] Batch cancelled - nothing sent",
               "=You declined (or the approval window expired). Nothing was sent.\n\nThe {{ $('Parse Compose').first().json.count }} contacts stay queued and will be offered again on the next run.", (12, 5))
    wf.add("Done: cancelled", "n8n-nodes-base.noOp", 1, {}, (13, 5))
    wf.link("Anything composed?", "Approval: review full copy", 0); wf.link("Approval: review full copy", "Approved?")
    wf.link("Approved?", "Abort run (declined)", 1); wf.chain("Abort run (declined)", "Notice: batch cancelled", "Done: cancelled")

    wf.add("Expand items", "n8n-nodes-base.code", 2, {"jsCode":
           "const src = $('Parse Compose').first().json;\nreturn (src.items || []).map(i => ({ json: { ...i, run_id: src.run_id } }));"}, (11, 4))
    wf.add("Loop Over Items", "n8n-nodes-base.splitInBatches", 3, {"batchSize": 1, "options": {}}, (12, 4))
    wf.link("Approved?", "Expand items", 0); wf.link("Expand items", "Loop Over Items")

    item_ids = "--run-id " + safe("$('Expand items').item.json.run_id") + " --prospect-id " + safe("$('Expand items').item.json.prospect_id", RX_PID)
    exec_node(wf, "Claim (atomic)", f"claim --run-id {safe('$json.run_id')} --prospect-id {safe('$json.prospect_id', RX_PID)}", (13, 4), once=False)
    parse_node(wf, "Parse Claim", (14, 4))
    if_bool(wf, "Claimed?", "$json.claimed === true", (15, 4))
    wf.add("Skip: already handled", "n8n-nodes-base.noOp", 1, {}, (16, 3))
    gmail_raw_send(wf, "Gmail: send raw MIME", (16, 4))
    exec_node(wf, "Record sent", f"record {item_ids} --status sent --message-id {safe('$json.id')} --thread-id {safe('$json.threadId')}{TEST}", (17, 4), once=False)
    wf.add("Random gap", "n8n-nodes-base.code", 2, {"jsCode": GAP_JS}, (18, 4))
    wf.add("Wait between sends", "n8n-nodes-base.wait", 1.1, {"amount": "={{ $json.gap }}", "unit": "seconds"}, (19, 4),
           webhookId=str(uuid.uuid4()))
    exec_node(wf, "Record failed", f"record {item_ids} --status failed {ERROR_ARG}{TEST}", (17, 5), once=False)
    wf.link("Loop Over Items", "Claim (atomic)", 1); wf.chain("Claim (atomic)", "Parse Claim", "Claimed?")
    wf.link("Claimed?", "Gmail: send raw MIME", 0); wf.link("Claimed?", "Skip: already handled", 1); wf.link("Skip: already handled", "Loop Over Items")
    wf.link("Gmail: send raw MIME", "Record sent", 0); wf.link("Gmail: send raw MIME", "Record failed", 1)
    wf.chain("Record sent", "Random gap", "Wait between sends", "Loop Over Items")
    wf.link("Record failed", "Loop Over Items")

    exec_node(wf, "Summary", f"summary --run-id {compose_run}", (13, 2))
    parse_node(wf, "Parse Summary", (14, 2))
    gmail_send(wf, "Summary email", "=[Outreach] Batch done: {{ $json.counts.sent || 0 }} sent, {{ $json.counts.send_failed || 0 }} failed",
               "={{ $json.html }}", (15, 2), html=True)
    exec_node(wf, "sheet-status", "sheet-status", (16, 2))
    parse_node(wf, "Parse sheet-status", (17, 2))
    code_node(wf, "Rows for sheet", SHEET_ROWS_JS, (18, 2))
    sheets_node(wf, "Sheet: write results", {
        "operation": "appendOrUpdate",
        "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": ["name", "company"], "schema": []},
        "options": {}}, (19, 2))
    wf.link("Loop Over Items", "Summary", 0)
    wf.chain("Summary", "Parse Summary", "Summary email", "sheet-status", "Parse sheet-status", "Rows for sheet", "Sheet: write results")
    wf.link("Notice: nothing verified", "sheet-status")
    return wf


def build_error():
    wf = WF("Recruiter Outreach - Error Handler", ERR_ID)
    wf.settings.pop("executionTimeout", None)
    wf.note("Error Workflow of Find & Send, Follow-ups, Intake & Verify and Bounce Scan. Emails you on any crash with the execution link.\n"
            "If a send loop died mid-batch: `python3 pipeline.py status`, then `python3 pipeline.py requeue` for stuck contacts.", (0, 0), 520, 160)
    wf.add("Error Trigger", "n8n-nodes-base.errorTrigger", 1, {}, (0, 2))
    wf.add("Email me the failure", "n8n-nodes-base.gmail", 2.1, {
        "sendTo": APPROVER, "subject": "=[Outreach] Workflow crashed: {{ $json.workflow.name }}", "emailType": "text",
        "message": "=Workflow: {{ $json.workflow.name }}\nExecution: {{ $json.execution.id }} ({{ $json.execution.mode }})\n"
                   "Open: {{ $json.execution.url }}\nLast node: {{ $json.execution.lastNodeExecuted }}\n\n"
                   "Error: {{ $json.execution.error.message }}\n\nStack:\n{{ ($json.execution.error.stack || '').slice(0, 1500) }}\n\n"
                   "No further emails are sent by a crashed run. Contacts already claimed but not recorded are released after 30 minutes "
                   "and re-offered on the next run.",
        "options": {"appendAttribution": False}}, (1, 2), credentials=GMAIL_CRED)
    wf.link("Error Trigger", "Email me the failure")
    return wf


def build_followups():
    wf = WF("Recruiter Outreach - Follow-ups", FU_ID, ERR_ID)
    # No execution timeout: n8n 2.x counts the time a run spends waiting (approval, gaps between sends) from
    # its original start, so a 590 s cap cancelled every batch approved after ~10 min, at the first gap.
    # The run stays bounded by approval_wait_hours and the batch size.
    wf.settings["executionTimeout"] = -1
    wf.note("## Follow-ups (daily 14:00 IST, one bump per person, ever)\n"
            "Up to `limit` 25 a day (engine `MAX_FOLLOWUPS_PER_DAY`, separate from the 50 first touches). Lists contacts sent 5+ business days "
            "ago -> approval email with the full copy -> for each: reads the Gmail thread; skips (and records) anyone who replied or bounced (delay notices are ignored); "
            "otherwise sends the bump inside the original thread (In-Reply-To + threadId), no attachment. Same claim/record/throttle safeguards "
            "as Find & Send. A non-empty `test_recipient` adds `--test` to followup-compose and record (the engine picks the test address).",
            (0, 0), 640, 260)
    triggers(wf, "Schedule daily 14:00 IST", "0 14 * * *", (0, 3))
    check = config_node(wf, (1, 3), [("limit", 25, "number")] + MAIL_CFG + BASE_CFG)
    exec_node(wf, "List due follow-ups", f"followups --limit {{{{ {CFG}.limit }}}}", (3, 3))
    parse_node(wf, "Parse due", (4, 3))
    if_bool(wf, "Any due?", "Number($json.count || 0) > 0", (5, 3))
    wf.add("Done: none due", "n8n-nodes-base.noOp", 1, {}, (6, 2))
    wf.chain(check, "List due follow-ups", "Parse due", "Any due?"); wf.link("Any due?", "Done: none due", 1)
    gmail_approval(wf, "Approval: review follow-ups", "=[Outreach] Approve {{ $json.count }} follow-ups - run {{ $json.run_id }}",
                   "={{ $json.digest }}", "=Send {{ $('Parse due').first().json.count }} follow-ups", (6, 3))
    if_bool(wf, "Approved?", "$json.data && $json.data.approved === true", (7, 3))
    wf.add("Done: cancelled", "n8n-nodes-base.noOp", 1, {}, (8, 4))
    wf.link("Any due?", "Approval: review follow-ups", 0); wf.link("Approval: review follow-ups", "Approved?"); wf.link("Approved?", "Done: cancelled", 1)
    wf.add("Expand due", "n8n-nodes-base.code", 2, {"jsCode":
           "const src = $('Parse due').first().json;\nreturn (src.items || []).map(i => ({ json: { ...i, run_id: src.run_id } }));"}, (8, 3))
    wf.add("Loop Over Items", "n8n-nodes-base.splitInBatches", 3, {"batchSize": 1, "options": {}}, (9, 3))
    wf.link("Approved?", "Expand due", 0); wf.link("Expand due", "Loop Over Items")
    wf.add("Gmail: read thread", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "GET", "url": "=https://gmail.googleapis.com/gmail/v1/users/me/threads/{{ $json.thread_id }}?format=metadata&metadataHeaders=From&metadataHeaders=Message-ID&metadataHeaders=Subject",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "gmailOAuth2", "options": {"timeout": 30000}},
        (10, 3), credentials=GMAIL_CRED, onError="continueErrorOutput", retryOnFail=True, maxTries=3, waitBetweenTries=5000)
    wf.add("Analyze thread", "n8n-nodes-base.code", 2, {"jsCode":
           "const t = $input.first().json;\nconst me = String($('Config').first().json.sender_address || '').toLowerCase();\n"
           "const msgs = t.messages || [];\nlet inReplyTo = null, replied = false, bounced = false;\n"
           "msgs.forEach((m, idx) => {\n  const h = {};\n  (m.payload && m.payload.headers || []).forEach(x => { h[String(x.name).toLowerCase()] = x.value; });\n"
           "  const from = String(h.from || '').toLowerCase(), subj = String(h.subject || '').toLowerCase();\n"
           "  if (idx === 0 && h['message-id']) inReplyTo = h['message-id'];\n"
           "  const ndr = from.includes('mailer-daemon') || from.includes('postmaster') || subj.includes('delivery status') || subj.includes('undeliverable');\n"
           f"  if (ndr && /{DELAY_RX}/i.test(subj + ' ' + String(m.snippet || ''))) return;\n  if (ndr) bounced = true;\n"
           "  else if (idx > 0 && from && !from.includes(me)) replied = true;\n});\n"
           "return [{ json: { ...$('Expand due').item.json, in_reply_to: inReplyTo, replied, bounced, message_count: msgs.length }, pairedItem: { item: 0 } }];"}, (11, 3))
    wf.add("Skip: thread unreadable", "n8n-nodes-base.noOp", 1, {}, (11, 4))
    wf.link("Loop Over Items", "Gmail: read thread", 1); wf.link("Gmail: read thread", "Analyze thread", 0)
    wf.link("Gmail: read thread", "Skip: thread unreadable", 1); wf.link("Skip: thread unreadable", "Loop Over Items")
    if_bool(wf, "Replied or bounced?", "$json.replied === true || $json.bounced === true", (12, 3))
    ids = f"--run-id {safe('$json.run_id')} --prospect-id {safe('$json.prospect_id', RX_PID)}"
    # Bounce -> `bounce --thread-id` (idempotent, filters delay notices); mark has no delay guard, so reply only.
    j_thread = f"(/{RX_ID}/.test(String($json.thread_id || '')) ? String($json.thread_id) : 'invalid-id')"
    j_run = f"(/{RX_ID}/.test(String($json.run_id || '')) ? String($json.run_id) : 'invalid-id')"
    j_pid = f"(/{RX_PID}/.test(String($json.prospect_id || '')) ? String($json.prospect_id) : 'invalid-id')"
    mark_cmd = (f"{{{{ $json.bounced ? 'bounce --thread-id ' + {j_thread} + ' --reason \"bounce detected before follow-up\"'"
                f" : 'mark --run-id ' + {j_run} + ' --prospect-id ' + {j_pid} + ' --status replied --note \"reply detected before follow-up\"' }}}}")
    exec_node(wf, "Mark replied/bounced", mark_cmd, (13, 2), once=False)
    wf.link("Analyze thread", "Replied or bounced?"); wf.link("Replied or bounced?", "Mark replied/bounced", 0); wf.link("Mark replied/bounced", "Loop Over Items")
    exec_node(wf, "Compose follow-up",
              f"followup-compose {ids}"
              "{{ $json.in_reply_to ? ' --in-reply-to \"' + String($json.in_reply_to).replace(/[^A-Za-z0-9@.<>_+=-]/g, '') + '\"' : '' }}"
              f"{TEST}", (13, 3), once=False)
    parse_node(wf, "Parse compose", (14, 3))
    if_bool(wf, "Composed?", "$json.composed === true", (15, 3))
    wf.add("Skip: not eligible", "n8n-nodes-base.noOp", 1, {}, (16, 2))
    due_run = safe("$('Expand due').item.json.run_id")
    due_ids = f"--run-id {due_run} --prospect-id " + safe("$('Expand due').item.json.prospect_id", RX_PID)
    exec_node(wf, "Claim (atomic)", f"claim --run-id {due_run} --prospect-id {safe('$json.prospect_id', RX_PID)} --kind followup", (16, 3), once=False)
    parse_node(wf, "Parse claim", (17, 3))
    if_bool(wf, "Claimed?", "$json.claimed === true", (18, 3))
    wf.add("Skip: already handled", "n8n-nodes-base.noOp", 1, {}, (19, 2))
    gmail_raw_send(wf, "Gmail: send follow-up", (19, 3), with_thread=True)
    exec_node(wf, "Record sent", f"record {due_ids} --kind followup --status sent --message-id {safe('$json.id')} "
                                 f"--thread-id {safe('$json.threadId')}{TEST}", (20, 3), once=False)
    wf.add("Random gap", "n8n-nodes-base.code", 2, {"jsCode": GAP_JS}, (21, 3))
    wf.add("Wait between sends", "n8n-nodes-base.wait", 1.1, {"amount": "={{ $json.gap }}", "unit": "seconds"}, (22, 3), webhookId=str(uuid.uuid4()))
    exec_node(wf, "Record failed", f"record {due_ids} --kind followup --status failed {ERROR_ARG}{TEST}", (20, 4), once=False)
    wf.link("Replied or bounced?", "Compose follow-up", 1); wf.chain("Compose follow-up", "Parse compose", "Composed?")
    wf.link("Composed?", "Claim (atomic)", 0); wf.link("Composed?", "Skip: not eligible", 1); wf.link("Skip: not eligible", "Loop Over Items")
    wf.chain("Claim (atomic)", "Parse claim", "Claimed?")
    wf.link("Claimed?", "Gmail: send follow-up", 0); wf.link("Claimed?", "Skip: already handled", 1); wf.link("Skip: already handled", "Loop Over Items")
    wf.link("Gmail: send follow-up", "Record sent", 0); wf.link("Gmail: send follow-up", "Record failed", 1)
    wf.chain("Record sent", "Random gap", "Wait between sends", "Loop Over Items")
    wf.link("Record failed", "Loop Over Items")
    exec_node(wf, "Status", "status", (10, 1))
    parse_node(wf, "Parse status", (11, 1))
    gmail_send(wf, "Summary email", "=[Outreach] Follow-up batch done",
               "=Follow-up run finished.\n\nSends today: {{ $json.sends_today }}\nPipeline status counts: {{ JSON.stringify($json.counts) }}\n"
               "Still due: {{ $json.followups_due }}", (12, 1))
    wf.link("Loop Over Items", "Status", 0); wf.chain("Status", "Parse status", "Summary email")
    return wf


def build_intake():
    wf = WF("Recruiter Outreach - Intake & Verify", INTAKE_ID, ERR_ID)
    wf.note("## Recruiter Outreach - Intake & Verify (every 20 min, 19:00-23:40 IST)\n"
            "1. Refreshes `inbox/sheet.csv` from the `Recruiter Outreach - Names` sheet: name, first/last name, company, job title, city, country, "
            "domain, tier, email hash, LinkedIn URL (not the email column: the engine finds every address). Rows that already carry a status were written back by the engine and are skipped; clear "
            "the status cell to import a row again.\n2. `pipeline.py intake`: LinkedIn DB import (read-only) -> sheet import -> bounded OSINT -> "
            "bounded re-verify, stopping before the time budget (max 500 s; the workflow timeout is 590 s).\n\nA sheet problem never blocks the "
            "LinkedIn intake. Failures email you once per evening (the 19:00 run) or on a manual run; crashes go to the Error Handler. If writing "
            f"the CSV fails with a file-access error, allow {OUTREACH_DIR} in `N8N_RESTRICT_FILE_ACCESS_TO`.", (0, 0), 700, 320)
    triggers(wf, "Schedule every 20 min 19:00-23:40 IST", "*/20 19-23 * * *", (0, 3))
    check = config_node(wf, (1, 3), [("intake_time_budget_s", 480, "number"), ("intake_probe_budget", 60, "number"),
                                     ("approver_email", APPROVER, "string")] + BASE_CFG + SHEET_CFG)
    sheets_node(wf, "Sheet: read names", {"operation": "read", "options": {}}, (3, 3), alwaysOutputData=True, onError="continueErrorOutput")
    code_node(wf, "Rows to CSV", ROWS_TO_CSV_JS, (4, 3))
    wf.add("CSV to file", "n8n-nodes-base.convertToFile", 1.1,
           {"operation": "toText", "sourceProperty": "csv", "options": {"fileName": "sheet.csv"}}, (5, 3))
    wf.add("Write inbox/sheet.csv", "n8n-nodes-base.readWriteFile", 1,
           {"operation": "write", "fileName": f"={{{{ {CFG}.outreach_dir }}}}/inbox/sheet.csv", "dataPropertyName": "data", "options": {}},
           (6, 3), onError="continueErrorOutput")
    exec_node(wf, "Intake + verify", f"intake --time-budget {{{{ {CFG}.intake_time_budget_s }}}} --probe-budget "
                                     f"{{{{ {CFG}.intake_probe_budget }}}} --sheet-csv inbox/sheet.csv", (7, 3))
    parse_node(wf, "Parse Intake", (8, 3))
    if_bool(wf, "Intake OK?", "$json.ok === true", (9, 3))
    wf.add("Done", "n8n-nodes-base.noOp", 1, {}, (10, 2))
    if_bool(wf, "Intake alert due?", ALERT_DUE, (10, 4))
    gmail_send(wf, "Alert: intake failed", "=[Outreach] Intake & Verify did not finish cleanly",
               "=The evening intake returned ok=false (sent once per evening, from the 19:00 run, or on a manual run).\n\n"
               "{{ JSON.stringify($('Parse Intake').first().json, null, 2).slice(0, 3000) }}\n\nExecution {{ $execution.id }}. "
               "Find & Send (09:30) only sends from the verified backlog, so fix this before then.", (11, 4))
    if_bool(wf, "Sheet alert due?", ALERT_DUE, (4, 5))
    gmail_send(wf, "Alert: sheet refresh failed", "=[Outreach] Names sheet not imported",
               "=Reading the 'Recruiter Outreach - Names' sheet or writing inbox/sheet.csv failed, so new sheet rows are not imported yet. The "
               "LinkedIn intake and verification still run.\n\nError: {{ String((typeof $json.error === 'string' ? $json.error : $json.error && ($json.error.message"
               " || JSON.stringify($json.error))) || 'no error text; open the execution in n8n').slice(0, 800) }}\nExecution {{ $execution.id }} (sent once per evening, from the "
               f"19:00 run, or on a manual run).\n\nA file-access error means n8n may not write {{{{ {CFG}.outreach_dir }}}}/inbox "
               "(N8N_RESTRICT_FILE_ACCESS_TO).", (5, 6), onError="continueRegularOutput")
    wf.chain(check, "Sheet: read names", "Rows to CSV", "CSV to file", "Write inbox/sheet.csv", "Intake + verify", "Parse Intake", "Intake OK?", "Done")
    wf.link("Sheet: read names", "Sheet alert due?", 1); wf.link("Write inbox/sheet.csv", "Sheet alert due?", 1)
    wf.link("Sheet alert due?", "Alert: sheet refresh failed", 0); wf.link("Sheet alert due?", "Intake + verify", 1); wf.link("Alert: sheet refresh failed", "Intake + verify")
    wf.link("Intake OK?", "Intake alert due?", 1); wf.link("Intake alert due?", "Alert: intake failed", 0); wf.link("Intake alert due?", "Done", 1)
    return wf


def build_bounce():
    wf = WF("Recruiter Outreach - Bounce Scan", BOUNCE_ID, ERR_ID)
    wf.note("## Recruiter Outreach - Bounce Scan (daily 21:00 IST)\n"
            f"Gmail search `{BOUNCE_QUERY}` (max 50); delay notices are ignored. Per bounce: `pipeline.py bounce --thread-id` (Gmail files the "
            "bounce in the original thread); if that matches nothing and the failed recipient could be parsed, `pipeline.py bounce --email`. Only "
            "validated ids and addresses reach the shell. Marked contacts feed the preflight bounce breaker. Emails a summary when notices were found.",
            (0, 0), 640, 280)
    triggers(wf, "Schedule daily 21:00 IST", "0 21 * * *", (0, 3))
    check = config_node(wf, (1, 3), [("approver_email", APPROVER, "string"), ("sender_address", APPROVER, "string")] + BASE_CFG)
    wf.add("Gmail: find bounces", "n8n-nodes-base.gmail", 2.1, {
        "resource": "message", "operation": "getAll", "returnAll": False, "limit": 50, "simple": True,
        "filters": {"q": BOUNCE_QUERY}, "options": {}}, (3, 3), credentials=GMAIL_CRED)
    code_node(wf, "Extract bounce keys", BOUNCE_EXTRACT_JS, (4, 3))
    wf.add("Loop Over Items", "n8n-nodes-base.splitInBatches", 3, {"batchSize": 1, "options": {}}, (5, 3))
    exec_node(wf, "Bounce by thread", f"bounce --thread-id {safe('$json.thread_id')} --reason gmail-ndr", (6, 4), once=False)
    code_node(wf, "Parse thread result", BY_THREAD_JS, (7, 4), per_item=True)
    if_bool(wf, "Try email?", f"!$json.matched && /{RX_EMAIL}/.test(String($json.email || ''))", (8, 4))
    exec_node(wf, "Bounce by email", f"bounce --email {safe('$json.email', RX_EMAIL, 'invalid@invalid.invalid')} --reason gmail-ndr", (9, 5), once=False)
    code_node(wf, "Parse email result", BY_EMAIL_JS, (10, 5), per_item=True)
    code_node(wf, "Bounce results", BOUNCE_REPORT_JS, (6, 2))
    gmail_send(wf, "Bounce summary email", "=[Outreach] Bounce scan: {{ $json.matched }} marked bounced, {{ $json.unmatched }} not matched",
               "=Bounce notices found (mailer-daemon / postmaster, last 2 days): {{ $json.found }}\n\n{{ $json.text }}\n\n"
               "Marked contacts count toward the preflight bounce breaker, which stops Find & Send when bounces pile up.\n'no match' means the "
               "notice is not for a pipeline email, or its thread and address are unknown to the engine.\nNotices stay in the 2-day window, so a "
               "bounce can be listed on two nights; marking is idempotent.", (7, 2))
    wf.chain(check, "Gmail: find bounces", "Extract bounce keys", "Loop Over Items")
    wf.link("Loop Over Items", "Bounce by thread", 1); wf.chain("Bounce by thread", "Parse thread result", "Try email?")
    wf.link("Try email?", "Bounce by email", 0); wf.link("Try email?", "Loop Over Items", 1); wf.chain("Bounce by email", "Parse email result", "Loop Over Items")
    wf.link("Loop Over Items", "Bounce results", 0); wf.link("Bounce results", "Bounce summary email")
    return wf


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for wf, fn in ((build_error(), "error_handler.json"), (build_main(), "find_and_send.json"), (build_followups(), "followups.json"),
                   (build_intake(), "intake_and_verify.json"), (build_bounce(), "bounce_scan.json")):
        p = wf.dump(OUT / fn)
        print(f"{fn}: {len(wf.nodes)} nodes, {sum(len(b) for c in wf.conn.values() for b in c['main'])} links")
