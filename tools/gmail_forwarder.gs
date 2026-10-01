/**
 * gmail_forwarder.gs — send emailed class materials to the coursework repo.
 *
 * Runs inside YOUR Google account (script.google.com). Every hour it looks for
 * role-information emails, and for each new one writes the attachments and a
 * small message.json into  inbox/<YYMMDD>-<message id>/  of your private data
 * repo. The nightly refresh files them into the right class folder.
 *
 * Your Gmail login never leaves Google. The only secret is a GitHub token that
 * can write to that one repo.
 *
 * Setup (once):
 *   1. Project Settings → Script properties → add
 *        GITHUB_TOKEN   fine-grained token, repository access: only the data repo,
 *                       permission "Contents: Read and write"
 *        REPO           e.g.  yourname/hbs-coursework-2026
 *        BRANCH         main            (optional)
 *   2. Run  checkSetup  once and approve the Gmail and external-request prompts.
 *   3. Run  installTrigger  once. Done.
 *
 * To forward a different kind of email, add a Gmail search to QUERIES.
 */

const QUERIES = [
  'subject:"CONFIDENTIAL ROLE INFORMATION" newer_than:10d',
];
const LABEL = 'hbs-forwarded';
const MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024;

function forwardClassEmails() {
  const cfg = config_();
  const label = GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
  let sent = 0;
  QUERIES.forEach(function (q) {
    GmailApp.search(q + ' -label:' + LABEL, 0, 25).forEach(function (thread) {
      let ok = true;
      thread.getMessages().forEach(function (msg) {
        try { forwardMessage_(msg, cfg); sent++; }
        catch (e) { ok = false; console.error('Failed: ' + msg.getSubject() + ' — ' + e); }
      });
      if (ok) thread.addLabel(label);     // a failed thread is retried next hour
    });
  });
  console.log('Forwarded ' + sent + ' message(s).');
}

function forwardMessage_(msg, cfg) {
  const stamp = Utilities.formatDate(msg.getDate(), 'America/New_York', 'yyMMdd');
  const dir = 'inbox/' + stamp + '-' + msg.getId() + '/';
  const names = [];
  msg.getAttachments({ includeInlineImages: false, includeAttachments: true }).forEach(function (att) {
    if (att.getSize() > MAX_ATTACHMENT_BYTES) { console.warn('Skipped large attachment ' + att.getName()); return; }
    const name = att.getName().replace(/[\\/:*?"<>|]/g, '-');
    put_(cfg, dir + name, att.getBytes(), 'inbox: ' + name);
    names.push(name);
  });
  const body = msg.getPlainBody() || '';
  const links = (body.match(/https?:\/\/[^\s<>")]+/g) || []).filter(function (v, i, a) { return a.indexOf(v) === i; });
  const meta = {
    id: msg.getId(), subject: msg.getSubject(), from: msg.getFrom(),
    date: msg.getDate().toISOString(), body: body, links: links, attachments: names,
  };
  // message.json goes last: the pipeline only routes folders that have it.
  put_(cfg, dir + 'message.json', Utilities.newBlob(JSON.stringify(meta, null, 2)).getBytes(),
       'inbox: ' + msg.getSubject());
}

function put_(cfg, path, bytes, message) {
  const url = 'https://api.github.com/repos/' + cfg.repo + '/contents/' +
              path.split('/').map(encodeURIComponent).join('/');
  const res = UrlFetchApp.fetch(url, {
    method: 'put',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + cfg.token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ message: message, branch: cfg.branch, content: Utilities.base64Encode(bytes) }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  if (code === 201 || code === 200) return;
  if (code === 422 && /sha/.test(res.getContentText())) return;      // already there from an earlier attempt
  throw new Error('GitHub ' + code + ' for ' + path + ': ' + res.getContentText().slice(0, 200));
}

function config_() {
  const p = PropertiesService.getScriptProperties();
  const token = p.getProperty('GITHUB_TOKEN'), repo = p.getProperty('REPO');
  if (!token || !repo) throw new Error('Set GITHUB_TOKEN and REPO in Project Settings → Script properties.');
  return { token: token, repo: repo, branch: p.getProperty('BRANCH') || 'main' };
}

/** Run once: checks the token can see the repo and shows what would be forwarded. */
function checkSetup() {
  const cfg = config_();
  const res = UrlFetchApp.fetch('https://api.github.com/repos/' + cfg.repo, {
    headers: { Authorization: 'Bearer ' + cfg.token, Accept: 'application/vnd.github+json' },
    muteHttpExceptions: true,
  });
  console.log('GitHub says ' + res.getResponseCode() + ' for ' + cfg.repo + (res.getResponseCode() === 200 ? ' — OK' : ' — check the token and REPO'));
  QUERIES.forEach(function (q) {
    GmailApp.search(q + ' -label:' + LABEL, 0, 25).forEach(function (t) {
      console.log('Would forward: ' + t.getFirstMessageSubject());
    });
  });
}

/** Run once: checks every hour from now on. */
function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'forwardClassEmails') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('forwardClassEmails').timeBased().everyHours(1).create();
  console.log('Hourly trigger installed.');
}
