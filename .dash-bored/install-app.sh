#!/bin/sh
# Opens the latest published macOS installer for this bundle's exact schema.
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
schema=$(sed -nE 's/^schemaVersion: *([0-9]+) *(#.*)?$/\1/p' "$bundle/dash-bored.yaml")
exec /usr/bin/osascript -l JavaScript - "$schema" <<'DASH_BORED_JXA'

function run(argv) {
  var app = Application.currentApplication();
  app.includeStandardAdditions = true;
  function quote(value) { return "'" + String(value).replace(/'/g, "'\\''") + "'"; }
  function shell(command) { return app.doShellScript(command); }
  var schema = Number(argv[0]);
  if (!Number.isSafeInteger(schema) || schema < 1) throw new Error("Cannot read a numeric schemaVersion from dash-bored.yaml.");
  if (shell("uname -s") !== "Darwin" || shell("uname -m") !== "arm64" || Number(shell("sw_vers -productVersion").split(".")[0]) < 14) {
    throw new Error("dash-bored requires an Apple Silicon Mac with macOS 14 or newer.");
  }
  var repository = "ArvidSU/dash-bored";
  var base = "https://github.com/" + repository + "/releases/download/";
  function curl(url) {
    return "/usr/bin/curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 " + quote(url);
  }
  function json(url) { return JSON.parse(shell(curl(url))); }
  function version(value) {
    var match = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?$/.exec(value);
    return match ? { numbers: match.slice(1, 4).map(Number), pre: match[4] } : null;
  }
  function compare(a, b) {
    var x = version(a), y = version(b);
    for (var i = 0; i < 3; i++) if (x.numbers[i] !== y.numbers[i]) return x.numbers[i] - y.numbers[i];
    if (x.pre === y.pre) return 0;
    if (!x.pre) return 1;
    if (!y.pre) return -1;
    var aa = x.pre.split("."), bb = y.pre.split(".");
    for (var j = 0; j < Math.max(aa.length, bb.length); j++) {
      var u = aa[j], v = bb[j];
      if (u === v) continue;
      if (u === undefined) return -1;
      if (v === undefined) return 1;
      var un = /^[0-9]+$/.test(u), vn = /^[0-9]+$/.test(v);
      if (un && vn) return Number(u) - Number(v);
      if (un !== vn) return un ? -1 : 1;
      return u < v ? -1 : 1;
    }
    return 0;
  }
  var releases = [];
  for (var page = 1; ; page++) {
    var batch = json("https://api.github.com/repos/" + repository + "/releases?per_page=100&page=" + page);
    if (!Array.isArray(batch)) throw new Error("Malformed GitHub release list.");
    releases = releases.concat(batch);
    if (batch.length < 100) break;
  }
  var candidates = releases.filter(function (r) {
    return r && !r.draft && typeof r.tag_name === "string" && r.tag_name[0] === "v" && version(r.tag_name.slice(1)) &&
      Array.isArray(r.assets) && r.assets.some(function (a) { return a.name === "dash-bored-release.json"; });
  }).sort(function (a, b) { return compare(b.tag_name.slice(1), a.tag_name.slice(1)); });
  var selected = null;
  for (var c = 0; c < candidates.length; c++) {
    var release = candidates[c];
    var m = json(base + release.tag_name + "/dash-bored-release.json");
    if (m.format !== 1 || m.product !== "dash-bored" || m.channel !== "canary" || m.platform !== "macos" || m.arch !== "arm64" || m.dashboardContract !== schema) continue;
    if ("v" + m.version !== release.tag_name || !m.dmg || !/^[a-zA-Z0-9_.-]+\.dmg$/.test(m.dmg.file) || !/^[a-f0-9]{64}$/.test(m.dmg.sha256) ||
        !release.assets.some(function (a) { return a.name === m.dmg.file; })) throw new Error("Invalid compatible release metadata or missing DMG.");
    selected = { tag: release.tag_name, dmg: m.dmg };
    break;
  }
  if (!selected) throw new Error("No published canary release opens dashboard schema " + schema + " without migration. See https://github.com/" + repository + "/releases.");
  var directory = shell("/usr/bin/mktemp -d /tmp/dash-bored-install.XXXXXX");
  var path = directory + "/" + selected.dmg.file;
  try {
    shell(curl(base + selected.tag + "/" + selected.dmg.file) + " --output " + quote(path));
    var digest = shell("/usr/bin/shasum -a 256 " + quote(path)).split(/\s+/)[0];
    if (digest !== selected.dmg.sha256) throw new Error("Installer SHA-256 mismatch; nothing was opened.");
    shell("/usr/bin/open " + quote(path));
    return "Opened verified dash-bored " + selected.tag + " for schema " + schema + ". Drag dash-bored-canary to Applications. Installer: " + path;
  } catch (error) {
    shell("/bin/rm -rf " + quote(directory));
    throw error;
  }
}
DASH_BORED_JXA
