/*
 * VooVr — deterministic transcript speaker attribution.
 *
 * Priority order (never falls back to alternating by line position):
 *   1. Explicit speaker labels  ("HR:", "Harshit:", "Harshit Rana:", "Employee:")
 *      — labels are stripped from the dialogue text and preserved as metadata.
 *   2. Known employee identity  — the workspace's employee record name; any
 *      label variant ("Harshit", "HARSHIT", "harshit rana", "Employee") maps
 *      to the same employee identity.
 *   3. HR labels                — "HR", "Human Resources", "HR Manager",
 *      "Interviewer", "Admin" normalize to the single HR speaker.
 *   4. Semantic inference       — only when a transcript has NO explicit
 *      labels at all. Low-confidence cues only; otherwise UNKNOWN.
 *   5. Consecutive turns        — a speaker may speak multiple times in a
 *      row; alternation is never forced.
 *
 * The spoken text is never modified: wording, Hinglish, punctuation and
 * order are preserved exactly; only speaker metadata is attached.
 */
(function (global) {
  'use strict';

  var HR_LABELS = ['hr', 'human resources', 'hr manager', 'interviewer', 'admin', 'recruiter'];
  var EMPLOYEE_LABELS = ['employee', 'emp', 'candidate'];

  // "Label: text". Label must start with a letter so times like "10:30 ..."
  // are never mistaken for a speaker label; only the FIRST colon splits.
  // "Speaker 1:" / "Person B:" style diarization labels are the one case where
  // digits are allowed inside a label.
  var SPEAKER_N = '(?:[Ss]peaker|SPEAKER|[Pp]erson|[Pp]articipant|Voice|Spk|SPK)\\s*[A-Za-z0-9]{1,2}';
  var LABEL_RE = new RegExp('^(' + SPEAKER_N + '|[A-Za-z][A-Za-z .\'\\u2019_-]{0,38})\\s*:\\s*(.*)$');
  var SPEAKER_N_RE = new RegExp('^' + SPEAKER_N + '$');

  // Capitalised words that open a sentence-style note ("Note: ...") rather
  // than naming a speaker; never treated as an inline speaker change.
  var NOT_A_SPEAKER = ['note', 'example', 'remember', 'important', 'reminder', 'summary',
    'action', 'question', 'answer', 'update', 'warning', 'tip', 'ps', 'agenda', 'topic'];

  // Role words that describe a job, not the employee being spoken to; used by
  // the two-party inference so "Manager:" is never promoted to employee.
  var ROLE_WORDS = ['manager', 'lead', 'director', 'supervisor', 'boss', 'ceo', 'cto', 'cfo',
    'head', 'vp', 'founder', 'mentor', 'coach', 'host', 'moderator', 'interviewer', 'recruiter',
    'hr', 'admin', 'teammate', 'colleague', 'peer'];

  var MAX_UTTERANCES = 500;
  var CONF_EXPLICIT = 1.0;
  var CONF_EXPLICIT_UNKNOWN_NAME = 0.95;
  var CONF_CONTINUATION = 0.9;
  var CONF_SEMANTIC_MAX = 0.6;
  var CONF_UNKNOWN = 0.2;

  function norm(s) {
    return String(s || '')
      .replace(/[\u2019']/g, '')
      .toLowerCase()
      .replace(/\s+/g, ' ')
      .trim();
  }

  function escapeRe(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // ── PRIORITY 2: known employee identity variants ──────────────────────
  // "Harshit Rana" → { 'harshit rana', 'harshit', 'rana' }
  function employeeVariants(employeeName) {
    var out = {};
    var n = norm(employeeName);
    if (!n) return out;
    out[n] = true;
    n.split(' ').forEach(function (part) {
      if (part.length >= 3) out[part] = true;
    });
    return out;
  }

  // Classify a candidate label against known identities.
  // Returns 'hr' | 'employee' | null (null = not a recognized role/person).
  function classifyLabel(rawLabel, empVariants) {
    var n = norm(rawLabel);
    if (!n) return null;
    if (empVariants[n]) return 'employee';
    if (EMPLOYEE_LABELS.indexOf(n) !== -1) return 'employee';
    if (HR_LABELS.indexOf(n) !== -1) return 'hr';
    return null;
  }

  // Proper-name style labels: "Harshit", "Harshit Rana", "Manager".
  function isProperNameLabel(rawLabel) {
    var label = String(rawLabel || '').trim();
    if (!label || label.length > 40) return false;
    if (SPEAKER_N_RE.test(label)) return true;
    var tokens = label.split(/\s+/);
    if (tokens.length > 4) return false;
    for (var i = 0; i < tokens.length; i++) {
      if (!/^[A-Z]/.test(tokens[i])) return false;
    }
    return true;
  }

  // Loose single-word lowercase labels ("manager:", "hr lead:") are accepted
  // only when the transcript is already clearly labelled elsewhere.
  function isLooseLabel(rawLabel) {
    var n = norm(rawLabel);
    return /^[a-z][a-z ]{0,19}$/.test(n);
  }

  function resolveSpeaker(rawLabel, empVariants, employeeName) {
    var cls = classifyLabel(rawLabel, empVariants);
    if (cls === 'employee') {
      return {
        speakerType: 'employee',
        speaker: (employeeName && String(employeeName).trim()) || 'Employee',
        confidence: CONF_EXPLICIT,
        source: 'explicit_label'
      };
    }
    if (cls === 'hr') {
      return {
        speakerType: 'hr',
        speaker: 'HR',
        confidence: CONF_EXPLICIT,
        source: 'explicit_label'
      };
    }
    // Unrecognized but structurally valid explicit label — preserve it
    // exactly as written rather than guessing HR vs employee.
    return {
      speakerType: 'unknown',
      speaker: String(rawLabel).trim(),
      confidence: CONF_EXPLICIT_UNKNOWN_NAME,
      source: 'explicit_label'
    };
  }

  // ── PRIORITY 4: semantic inference (ONLY when no labels exist) ────────
  function inferSemantic(line, firstNames) {
    var lower = norm(line);
    for (var i = 0; i < firstNames.length; i++) {
      if (new RegExp('\\b' + escapeRe(firstNames[i]) + '\\b').test(lower)) {
        return { speakerType: 'hr', speaker: 'HR', confidence: 0.6, source: 'semantic' };
      }
    }
    if (/\?\s*$/.test(line) && /\b(you|your|tum|tumhara|tumhari|tumne|aap|aapka|aapki|aapne)\b/.test(lower)) {
      return { speakerType: 'hr', speaker: 'HR', confidence: 0.55, source: 'semantic' };
    }
    if (/^(so|okay|ok|alright|got it|understood|i see|great|good|thanks|thank you|right|hmm|fair enough|noted)\b/.test(lower)) {
      return { speakerType: 'hr', speaker: 'HR', confidence: 0.5, source: 'semantic' };
    }
    if (!/\?\s*$/.test(line) && /\b(main|maine|mujhe|mera|meri|i am|i've|i feel|my )\b/.test(lower)) {
      return { speakerType: 'employee', speaker: null, confidence: 0.5, source: 'semantic' };
    }
    return { speakerType: 'unknown', speaker: null, confidence: CONF_UNKNOWN, source: 'none' };
  }

  // Split into non-empty raw lines (empty lines ignored entirely).
  function rawLinesOf(text) {
    return String(text || '').split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  // ── Label-format normalisation ─────────────────────────────────────────
  // Real transcripts rarely look like a clean "Label: text". This rewrites the
  // common variants into "Label: text" so the label logic below sees them:
  //   "[00:12] HR: ..."   "**HR:** ..."   "[HR]: ..."   "HR - ..."
  //   "Sakshi (Employee): ..."   "Priya (HR): ..."
  // Only the label prefix is rewritten; the spoken text is never touched. If a
  // line does not end up as a valid "Label: text", the ORIGINAL line is kept.
  var NAME_CH = "[A-Za-z][A-Za-z .'\\u2019_-]{0,38}";
  var TS_RE = /^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?[\])]?\s*[-–—]?\s*(?=\S)/;
  var EMPH_RE = /^\s*[*_]{1,2}([^*_]{1,45}?)[*_]{1,2}/;
  var BRACKET_RE = new RegExp('^\\s*\\[(' + NAME_CH + ')\\]\\s*([:\\-\\u2013\\u2014]?)\\s*');
  var PAREN_RE = new RegExp('^(' + NAME_CH + '?)\\s*\\(([^)]{1,30})\\)\\s*[:\\-\\u2013\\u2014]\\s*(.*)$');
  var DASH_RE = new RegExp('^(' + NAME_CH + '?)\\s+[-\\u2013\\u2014]\\s+(.*)$');

  function normalizeLabelPrefix(line, empVariants) {
    var w = line.replace(TS_RE, '');
    w = w.replace(EMPH_RE, '$1');
    var m = w.match(BRACKET_RE);
    if (m && (m[2] === ':' || classifyLabel(m[1], empVariants))) {
      w = m[1].trim() + ': ' + w.slice(m[0].length);
    }
    m = w.match(PAREN_RE);
    if (m) {
      var outer = m[1].trim();
      var inner = m[2].trim();
      var label = classifyLabel(outer, empVariants) ? outer : (classifyLabel(inner, empVariants) ? inner : outer);
      w = label + ': ' + m[3];
    }
    m = w.match(DASH_RE);
    if (m && classifyLabel(m[1], empVariants)) {
      w = m[1].trim() + ': ' + m[2];
    }
    return LABEL_RE.test(w) ? w : line;
  }

  // A whole conversation pasted as ONE paragraph ("HR: ... Sakshi: ... HR: ...")
  // is split at each speaker change. A change is only recognised right after a
  // sentence end, and only for known HR/employee labels, or for a capitalised
  // name when the paragraph demonstrably uses HR-style labels.
  function splitInlineTurns(line, empVariants) {
    var known = HR_LABELS.concat(EMPLOYEE_LABELS, Object.keys(empVariants))
      .sort(function (a, b) { return b.length - a.length; })
      .map(escapeRe);
    // A mid-paragraph timestamp before the label ("... ok. [00:20] Sakshi: ...")
    // is dropped along with the break; timestamps are not dialogue.
    var END = '([.?!\\u2026\\u0964"\\u201d])\\s+(?:[\\[(]\\d{1,2}:\\d{2}(?::\\d{2})?[\\])]\\s*)?';
    var out = line.replace(
      new RegExp(END + '((?:' + known.join('|') + '))\\s*:', 'gi'),
      function (m0, p, lab) { return p + '\n' + lab + ':'; }
    );
    var usesRoleLabels = new RegExp('(^|\\n)\\s*(?:' + known.join('|') + ')\\s*:', 'i').test(out);
    if (usesRoleLabels) {
      out = out.replace(
        new RegExp(END + "([A-Z][A-Za-z'\\u2019-]{1,20}(?:\\s[A-Z][A-Za-z'\\u2019-]{1,20})?)\\s*:(?=\\s)", 'g'),
        function (m0, p, lab, offset, str) {
          if (NOT_A_SPEAKER.indexOf(norm(lab)) !== -1) return m0;
          // "Dr. Rao:" / "Mr. Shah:" — the full stop belongs to a title.
          if (p === '.' && /\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|St)$/.test(str.slice(0, offset))) return m0;
          return p + '\n' + lab + ':';
        }
      );
    }
    return out.split('\n');
  }

  function normalizeTurns(rawLines, empVariants) {
    var out = [];
    rawLines.forEach(function (line) {
      splitInlineTurns(line, empVariants).forEach(function (piece) {
        piece = piece.trim();
        if (piece) out.push(normalizeLabelPrefix(piece, empVariants));
      });
    });
    return out;
  }

  // HR conversation with exactly ONE other named speaker whose name did not
  // match the employee record (renamed, nickname, no record loaded): that
  // person is the employee. The written name is kept; role words ("Manager")
  // and 3+ party transcripts are left untouched as "unknown".
  function inferTwoParty(utterances) {
    if (utterances.some(function (u) { return u.speakerType === 'employee'; })) return;
    if (!utterances.some(function (u) { return u.speakerType === 'hr'; })) return;
    var names = {};
    utterances.forEach(function (u) {
      if (u.speakerType === 'unknown' && u.source !== 'none') names[norm(u.speaker)] = true;
    });
    var keys = Object.keys(names);
    if (keys.length !== 1) return;
    var only = keys[0];
    if (!only || ROLE_WORDS.indexOf(only) !== -1) return;
    if (utterances.some(function (u) { return u.speakerType === 'unknown' && u.source === 'none'; })) return;
    utterances.forEach(function (u) {
      if (u.speakerType === 'unknown' && norm(u.speaker) === only) {
        u.speakerType = 'employee';
        u.confidence = 0.8;
        u.source = u.source === 'explicit_label_continuation' ? 'inferred_two_party_continuation' : 'inferred_two_party';
      }
    });
  }

  function parse(text, options) {
    options = options || {};
    var employeeName = (options.employeeName || '').trim();
    var duration = Number(options.duration) || 0;
    var empVariants = employeeVariants(employeeName);
    var firstNames = Object.keys(empVariants).filter(function (v) { return v.indexOf(' ') === -1; });
    var ctx = { employeeName: employeeName, duration: duration, empVariants: empVariants, firstNames: firstNames };

    var rawLines = rawLinesOf(text);
    if (!rawLines.length) return { lines: [], hasExplicitLabels: false };

    // Try the label-normalised form first. If that still is not an explicitly
    // labelled transcript, fall back to the untouched lines so unlabelled
    // text is preserved verbatim exactly as before.
    var result = parseCore(normalizeTurns(rawLines, empVariants), ctx);
    if (!result.hasExplicitLabels) result = parseCore(rawLines, ctx);
    return result;
  }

  function parseCore(rawLines, ctx) {
    var employeeName = ctx.employeeName;
    var duration = ctx.duration;
    var empVariants = ctx.empVariants;
    var firstNames = ctx.firstNames;

    // Pass 1 — collect label candidates, then decide globally whether this
    // transcript is explicitly labelled. A transcript counts as labelled if
    // ANY candidate is a recognized identity (HR / employee variants) or at
    // least TWO distinct proper-name labels exist. A single stray
    // "Note:"-style line never flips an unlabelled transcript.
    var parsed = rawLines.map(function (line) {
      var entry = { labelled: false, kind: null, label: '', text: line, raw: line };
      var m = line.match(LABEL_RE);
      if (!m) return entry;
      var label = m[1].trim();
      var rest = m[2].trim();
      var recognized = !!classifyLabel(label, empVariants);
      if (recognized || isProperNameLabel(label)) {
        entry.labelled = true;
        entry.kind = 'strict';
        entry.label = label;
        entry.text = rest;
        entry.recognized = recognized;
      } else if (isLooseLabel(label)) {
        // Valid only if the transcript is labelled elsewhere; otherwise the
        // colon belongs to the dialogue and the line stays untouched.
        entry.labelled = true;
        entry.kind = 'loose';
        entry.label = label;
        entry.text = rest;
      }
      return entry;
    });

    var recognizedExists = parsed.some(function (p) { return p.recognized; });
    var properLabels = {};
    parsed.forEach(function (p) {
      if (p.labelled && p.kind === 'strict' && !p.recognized) {
        properLabels[norm(p.label)] = true;
      }
    });
    var hasExplicitLabels =
      recognizedExists || Object.keys(properLabels).length >= 2;
    var looseAllowed = hasExplicitLabels;

    var utterances = [];
    function push(speakerMeta, textLine) {
      utterances.push({
        idx: utterances.length,
        text: textLine,
        speaker: speakerMeta.speaker,
        speakerType: speakerMeta.speakerType,
        confidence: speakerMeta.confidence,
        source: speakerMeta.source
      });
    }

    if (hasExplicitLabels) {
      // ── PRIORITIES 1–3 + 5: label-driven, consecutive turns preserved ──
      var active = null;     // speaker awaiting dialogue after a bare "Label:" line
      for (var i = 0; i < parsed.length; i++) {
        var p = parsed[i];
        if (p.labelled && (p.kind === 'strict' || (p.kind === 'loose' && looseAllowed))) {
          active = resolveSpeaker(p.label, empVariants, employeeName);
          if (p.text) push(active, p.text); // bare label keeps waiting
        } else if (active) {
          // Unlabelled continuation/wrap line inherits the active speaker.
          push({
            speakerType: active.speakerType,
            speaker: active.speaker,
            confidence: CONF_CONTINUATION,
            source: active.source === 'explicit_label' ? 'explicit_label_continuation' : active.source
          }, p.text);
        } else {
          push({ speakerType: 'unknown', speaker: 'Unknown', confidence: CONF_UNKNOWN, source: 'none' }, p.text);
        }
      }
    } else {
      // ── PRIORITY 4: semantic attribution, UNKNOWN over wrong guesses ──
      // Original lines are kept verbatim — no label stripping happened.
      for (var j = 0; j < parsed.length; j++) {
        var inferred = inferSemantic(parsed[j].raw, firstNames);
        if (!inferred.speaker && inferred.speakerType === 'employee') {
          inferred.speaker = employeeName || 'Employee';
        } else if (!inferred.speaker) {
          inferred.speaker = 'Unknown';
        }
        push(inferred, parsed[j].raw);
      }
    }

    if (hasExplicitLabels) inferTwoParty(utterances);

    if (utterances.length > MAX_UTTERANCES) utterances = utterances.slice(0, MAX_UTTERANCES);

    // Proportional timestamps (same model as before: chars share of duration).
    var totalChars = utterances.reduce(function (a, u) { return a + u.text.length; }, 0) || 1;
    var cursor = 0;
    utterances.forEach(function (u) {
      u.time = duration * (cursor / totalChars);
      cursor += u.text.length;
    });

    return { lines: utterances, hasExplicitLabels: hasExplicitLabels };
  }

  var api = { parse: parse };
  global.VooVrTranscript = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
