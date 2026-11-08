// The Claude button: opens a new session in the Claude desktop app's Code tab,
// in the note's project folder, with the note already typed into the prompt
// box, so Greg can add his answer and send it. The app takes
// claude://code/new?q=PROMPT&folder=PATH (up to 14336 characters of prompt).
// Laptop only: the phone has no Code tab to open.
const CLAUDE_HOME = "/home/gregory";  // anything else: the session moves itself (the rule in ~/.claude/CLAUDE.md)
const CLAUDE_FOLDERS = {
  "Minichord": "/home/gregory/minichord",
  "minicontrol": "/home/gregory/minicontrol",
  "Plates": "/home/gregory/minichord-harp-plates",
  "Repairs": "/mnt/keyandcable-files/00_Schematics+Manuals",
  "PCBs": "/mnt/keyandcable-files/00_KiCAD_Projects",
  "Shop app": "/mnt/keyandcable-files/key&cable",
  "Business": "/mnt/keyandcable-files/key&cable",
  "Website": "/mnt/keyandcable-files/key&cable",
  "Blog": "/mnt/keyandcable-files/key&cable",
};
const CLAUDE_HERE = /Linux/.test(navigator.userAgent) && !/Android/.test(navigator.userAgent);
const CLAUDE_PROMPT_MAX = 13500;  // under the app's 14336; the rest is a gtd show away

// A cut that splits an emoji in two would make encodeURIComponent throw.
function claudeCut(s, max) {
  if (s.length <= max) return s;
  s = s.slice(0, max);
  return /[\uD800-\uDBFF]$/.test(s) ? s.slice(0, -1) : s;
}

// The link, or null if the note can't be made into one (the page goes on without it).
function claudeLink(n) {
  try { return claudeHref(n); } catch (e) { return null; }
}
function claudeHref(n) {
  const id8 = n.id.slice(0, 8), title = n.title || n.body;
  const lines = [`Tracker item ${id8}: ${title}`];
  if (n.body && n.body !== title) lines.push(n.body);
  if (n.why) lines.push(`On the list because: ${n.why}`);
  if (n.note) lines.push(`Note: ${n.note}`);
  const tags = [n.when_tag, ...n.what, ...n.who.map(t => "who " + t), ...n.where.map(t => "where " + t)];
  if (n.on_day) tags.push("dated " + n.on_day);
  if (tags.filter(Boolean).length) lines.push(tags.filter(Boolean).join(" · "));
  if (n.details) lines.push("Details:\n" + n.details);
  const ask = ["", `Help me answer this tracker item. \`gtd show ${id8}\` has the latest. Record what we settle in its`
    + ` Details with \`gtd edit\` (keep what's there), and tick it with \`gtd done\` once I say it's done.`,
    "", "My answer: "].join("\n");
  const more = "\n(cut short: gtd show has the rest)";
  let item = lines.join("\n");
  if (item.length + ask.length > CLAUDE_PROMPT_MAX) item = claudeCut(item, CLAUDE_PROMPT_MAX - ask.length - more.length) + more;
  const folder = n.what.map(t => CLAUDE_FOLDERS[t]).find(Boolean) || CLAUDE_HOME;
  return `claude://code/new?q=${encodeURIComponent(item + "\n" + ask)}&folder=${encodeURIComponent(folder)}`;
}
