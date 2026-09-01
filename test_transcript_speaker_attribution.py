"""Speaker attribution regression tests for the Conversation Workspace.

Covers the priority system implemented in static/transcript-parser.js and
wired into static/conversation-workspace.html:

  PRIORITY 1 - explicit speaker labels are preserved exactly
  PRIORITY 2 - known employee identity (workspace employee record name)
  PRIORITY 3 - HR labels normalize to the single HR speaker
  PRIORITY 4 - semantic inference ONLY when no labels exist; UNKNOWN is
               preferred over a confident wrong guess; never alternation
  PRIORITY 5 - consecutive turns of one speaker are preserved

Two layers of tests:
  1. Runtime tests that execute the real parser via node (skipped when node
     is unavailable) - including the full exact regression transcript.
  2. Source-inspection tests that keep conversation-workspace.html honest:
     no alternating assignment, delegation to the parser, raw transcript
     preserved for export/moments, search/filter/copy intact.
"""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
_PARSER_JS = os.path.abspath(os.path.join(_ROOT, "static", "transcript-parser.js"))
_WORKSPACE_HTML = os.path.join(_ROOT, "static", "conversation-workspace.html")

EMPLOYEE_NAME = "Harshit Rana"

# -- EXACT regression transcript from the bug report ------------------------
REGRESSION_TRANSCRIPT = "\n".join(
    [
        "HR: Harshit, ek minute baat kar sakte ho?",
        "Harshit: Haan sure, kya hua?",
        "HR: Bas ek small check-in tha. Pichle kuch weeks se tum thode quiet lag rahe ho. Work-wise sab theek chal raha hai?",
        "Harshit: Haan, work toh theek hai. Bas thoda workload zyada ho gaya hai recently.",
        "HR: Hmm, workload kis part mein zyada feel ho raha hai? Deadlines ya overall tasks?",
        "Harshit: Mostly deadlines. Ek task finish karta hoon toh doosra already aa jata hai. Especially last two weeks mein kaafi back-to-back tha.",
        "HR: Got it. Kya manager se workload ke baare mein discuss kiya?",
        "Harshit: Nahi, honestly nahi. Socha manage ho jayega.",
        "HR: Fair enough. But agar consistently workload ki wajah se pressure aa raha hai toh early stage pe bolna better hota hai.",
        "Harshit: Haan, that's true. Main bhi thoda notice kar raha hoon ki lately work ke baad kaafi drained feel hota hai.",
        "HR: Understood. Aur team environment ya manager ke saath koi issue?",
        "Harshit: Nahi, aisa koi major issue nahi hai. Team actually supportive hai.",
        "Harshit: Bas kabhi-kabhi priorities clear nahi hoti.",
        "HR: That's useful feedback. Agar priorities clear ho jaayein toh workload thoda manageable lagega?",
        "Harshit: Haan, definitely.",
        "HR: Okay. Main ye point note kar leti hoon.",
        "HR: Aur ek cheez — agar tumhe lage workload genuinely unrealistic ho raha hai, directly bol dena.",
        "Harshit: Okay, that's actually reassuring.",
        "HR: Good. Tumhare side se aur kuch hai jo company better kar sakti hai?",
        "Harshit: Maybe regular one-on-one check-ins helpful honge.",
        "Harshit: Har baar koi problem hone ka wait na karna pade.",
        "HR: That's a good suggestion. I'll bring it up with the team.",
        "Harshit: Cool, thanks.",
        "HR: No problem.",
        "HR: Aur seriously, workload manageable nahi lag raha ho toh bata dena.",
        "Harshit: Haan, I'll do that. Thanks for checking in.",
        "HR: Anytime.",
    ]
)

EXPECTED_SEQUENCE_UPPER = [
    "HR", "HARSHIT RANA", "HR", "HARSHIT RANA", "HR", "HARSHIT RANA",
    "HR", "HARSHIT RANA", "HR", "HARSHIT RANA", "HR", "HARSHIT RANA",
    "HARSHIT RANA", "HR", "HARSHIT RANA", "HR", "HR", "HARSHIT RANA",
    "HR", "HARSHIT RANA", "HARSHIT RANA", "HR", "HARSHIT RANA", "HR",
    "HR", "HARSHIT RANA", "HR",
]

_NODE = shutil.which("node")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _parse(transcript, employee_name=EMPLOYEE_NAME, duration=120):
    """Run the real parser under node and return its JSON output."""
    if not _NODE:
        pytest.skip("node is not available")
    tmpdir = tempfile.mkdtemp(prefix="voovr_parser_")
    cfg_path = os.path.join(tmpdir, "cfg.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"transcript": transcript, "employee_name": employee_name, "duration": duration},
            fh,
            ensure_ascii=True,
        )
    script = (
        "const fs=require('fs');"
        "const parser=require(process.argv[1]);"
        "const cfg=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));"
        "const out=parser.parse(cfg.transcript,"
        "{employeeName:cfg.employee_name,duration:cfg.duration});"
        "process.stdout.write(JSON.stringify(out));"
    )
    proc = subprocess.run(
        [_NODE, "-e", script, _PARSER_JS, cfg_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, "parser crashed:\n" + proc.stderr
    return json.loads(proc.stdout)


def _speakers(result):
    return [line["speaker"] for line in result["lines"]]


def _types(result):
    return [line["speakerType"] for line in result["lines"]]


def _texts(result):
    return [line["text"] for line in result["lines"]]


# ---------------------------------------------------------------------------
# 1. THE EXACT REGRESSION TRANSCRIPT
# ---------------------------------------------------------------------------


class TestExactRegressionTranscript:
    def test_exact_speaker_sequence_preserved(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        assert [s.upper() for s in _speakers(result)] == EXPECTED_SEQUENCE_UPPER

    def test_turn_count_matches_transcript_lines(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        assert len(result["lines"]) == len(REGRESSION_TRANSCRIPT.splitlines()) == 27

    def test_every_turn_uses_explicit_label_source_with_max_confidence(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        for line in result["lines"]:
            assert line["source"] == "explicit_label"
            assert line["confidence"] == pytest.approx(1.0)

    def test_consecutive_turns_appear_exactly_where_transcript_has_them(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        types = _types(result)
        consecutive_pairs = sum(
            1 for i in range(1, len(types)) if types[i] == types[i - 1]
        )
        # Lines 12+13, 16+17, 20+21, 24+25 (1-based) are same-speaker pairs.
        assert consecutive_pairs == 4

    def test_labels_stripped_from_displayed_dialogue(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        for text in _texts(result):
            assert not text.startswith("HR:")
            assert not text.startswith("Harshit:")
            assert not text.startswith("Harshit Rana:")

    def test_spoken_wording_unchanged_line_by_line(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        expected_texts = [
            line.split(":", 1)[1].strip() for line in REGRESSION_TRANSCRIPT.splitlines()
        ]
        assert _texts(result) == expected_texts

    def test_hinglish_punctuation_and_order_intact(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        joined = " ".join(_texts(result))
        for phrase in (
            "ek minute baat kar sakte ho?",
            "Pichle kuch weeks se tum thode quiet lag rahe ho.",
            "Ek task finish karta hoon toh doosra already aa jata hai.",
            "kabhi-kabhi priorities clear nahi hoti.",
            "Aur ek cheez — agar tumhe lage workload genuinely unrealistic ho raha hai, directly bol dena.",
        ):
            assert phrase in joined

    def test_consecutive_employee_turns_render_back_to_back(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        speakers = [s.upper() for s in _speakers(result)]
        assert speakers[11] == speakers[12] == "HARSHIT RANA"
        assert speakers[19] == speakers[20] == "HARSHIT RANA"

    def test_consecutive_hr_turns_render_back_to_back(self):
        result = _parse(REGRESSION_TRANSCRIPT)
        speakers = [s.upper() for s in _speakers(result)]
        assert speakers[15] == speakers[16] == "HR"
        assert speakers[23] == speakers[24] == "HR"

    def test_analysis_evidence_quote_maps_to_employee_utterance(self):
        quote = "Bas kabhi-kabhi priorities clear nahi hoti."
        result = _parse(REGRESSION_TRANSCRIPT)
        raw_lower = REGRESSION_TRANSCRIPT.lower()
        rel = raw_lower.find(quote.lower())
        assert rel >= 0
        # Walk raw non-empty lines alongside utterances (1:1 for labelled
        # lines) to find which utterance's source span holds the evidence.
        cursor = 0
        matched = None
        raw_lines = [l for l in REGRESSION_TRANSCRIPT.splitlines() if l.strip()]
        for i, raw_line in enumerate(raw_lines):
            start = raw_lower.find(raw_line.lower(), cursor)
            end = start + len(raw_line)
            cursor = end
            if start <= rel < end:
                matched = result["lines"][i]
                break
        assert matched is not None
        assert matched["speakerType"] == "employee"
        assert matched["speaker"] == EMPLOYEE_NAME


# ---------------------------------------------------------------------------
# 2. EXPLICIT LABELS ALWAYS WIN (PRIORITIES 1-3, NEVER ALTERNATION)
# ---------------------------------------------------------------------------


class TestExplicitLabelsWin:
    def test_priority5_example_consecutive_turns_exact(self):
        transcript = "\n".join(
            [
                "HR: How has work been?",
                "HR: And how are the deadlines?",
                "Harshit: Deadlines are actually the main issue.",
                "Harshit: Ek task finish karta hoon toh doosra aa jata hai.",
                "HR: Got it. Has this been happening recently?",
            ]
        )
        result = _parse(transcript)
        assert _types(result) == ["hr", "hr", "employee", "employee", "hr"]
        assert [s.upper() for s in _speakers(result)] == [
            "HR", "HR", "HARSHIT RANA", "HARSHIT RANA", "HR",
        ]

    def test_five_identical_labels_in_a_row_never_alternated(self):
        transcript = "\n".join("HR: line number %d" % i for i in range(1, 6))
        result = _parse(transcript)
        assert set(_types(result)) == {"hr"}
        assert all(s == "HR" for s in _speakers(result))

    def test_four_employee_labels_in_a_row_never_alternated(self):
        transcript = "\n".join("Employee: answer %d" % i for i in range(1, 5))
        result = _parse(transcript, employee_name=EMPLOYEE_NAME)
        assert set(_types(result)) == {"employee"}
        assert all(s == EMPLOYEE_NAME for s in _speakers(result))

    @pytest.mark.parametrize(
        "label",
        [
            "hr:", "HR:", "hR:", "Hr :",
            "human resources:", "Human Resources:",
            "hr manager:", "interviewer:", "Interviewer:", "admin:",
        ],
    )
    def test_hr_label_variants_normalize_to_hr_identity(self, label):
        result = _parse("%s hello there" % label)
        assert result["hasExplicitLabels"] is True
        assert len(result["lines"]) == 1
        assert result["lines"][0]["speakerType"] == "hr"
        assert result["lines"][0]["speaker"] == "HR"
        assert result["lines"][0]["text"] == "hello there"

    @pytest.mark.parametrize(
        "label",
        ["harshit:", "HARSHIT:", "Harshit:", "harshit rana:", "HARSHIT RANA:", "Harshit Rana:"],
    )
    def test_employee_name_variants_map_to_known_employee(self, label):
        result = _parse("%s my take on this" % label)
        assert result["lines"][0]["speakerType"] == "employee"
        assert result["lines"][0]["speaker"] == EMPLOYEE_NAME
        assert result["lines"][0]["confidence"] == pytest.approx(1.0)

    @pytest.mark.parametrize("label", ["Employee:", "employee:", "EMPLOYEE:", "Candidate:"])
    def test_generic_employee_label_resolves_to_actual_name(self, label):
        result = _parse("%s work is heavy right now" % label)
        assert result["lines"][0]["speakerType"] == "employee"
        assert result["lines"][0]["speaker"] == EMPLOYEE_NAME

    def test_harshit_vs_harshit_rana_same_identity_across_turns(self):
        transcript = "Harshit: first\nHarshit Rana: second\nHARSHIT: third"
        result = _parse(transcript)
        assert set(_speakers(result)) == {EMPLOYEE_NAME}

    def test_generic_employee_label_without_known_name_stays_generic(self):
        result = _parse("Employee: I am tired", employee_name="")
        assert result["lines"][0]["speakerType"] == "employee"
        assert result["lines"][0]["speaker"] == "Employee"

    def test_unknown_explicit_label_preserved_exactly_not_forced(self):
        transcript = "Manager: how is the team?\nRahul: it is fine."
        result = _parse(transcript)
        assert result["hasExplicitLabels"] is True
        assert result["lines"][0]["speaker"] == "Manager"
        assert result["lines"][0]["speakerType"] == "unknown"
        assert result["lines"][1]["speaker"] == "Rahul"
        # Never flipped to HR/Employee by position.
        assert all(t == "unknown" for t in _types(result))

    def test_loose_lowercase_custom_label_in_labelled_transcript(self):
        transcript = "HR: kickoff notes\nmanager: action items attached"
        result = _parse(transcript)
        assert result["lines"][0]["speakerType"] == "hr"
        assert result["lines"][1]["speaker"] == "manager"
        assert result["lines"][1]["speakerType"] == "unknown"

    def test_bare_label_line_attributes_following_lines(self):
        transcript = "HR:\nHello team\nSecond thought on policies"
        result = _parse(transcript)
        assert len(result["lines"]) == 2
        assert all(t == "hr" for t in _types(result))
        assert _texts(result) == ["Hello team", "Second thought on policies"]

    def test_label_without_space_after_colon(self):
        result = _parse("HR:hello there\nHarshit:hi")
        assert _types(result) == ["hr", "employee"]
        assert _texts(result) == ["hello there", "hi"]

# ---------------------------------------------------------------------------
# 3. SEMANTIC FALLBACK — only when labels are completely absent
# ---------------------------------------------------------------------------


class TestSemanticFallbackNoLabels:
    def test_addressing_employee_by_name_is_hr_speaking(self):
        result = _parse("Harshit, ek minute baat kar sakte ho?", employee_name=EMPLOYEE_NAME)
        line = result["lines"][0]
        assert line["speakerType"] == "hr"
        assert line["source"] == "semantic"
        assert line["confidence"] <= 0.6

    def test_second_person_question_is_hr_speaking(self):
        result = _parse("How has work been for you lately?")
        assert result["hasExplicitLabels"] is False
        assert result["lines"][0]["speakerType"] == "hr"
        assert result["lines"][0]["source"] == "semantic"

    def test_first_person_hinglish_statement_is_employee(self):
        result = _parse("Main thoda drained feel kar raha hoon these days.")
        assert result["lines"][0]["speakerType"] == "employee"
        assert result["lines"][0]["speaker"] == EMPLOYEE_NAME
        assert result["lines"][0]["confidence"] <= 0.6

    def test_low_confidence_line_marked_unknown(self):
        result = _parse("Deadlines tight hain.")
        line = result["lines"][0]
        assert line["speakerType"] == "unknown"
        assert line["speaker"] == "Unknown"
        assert line["source"] == "none"
        assert line["confidence"] == pytest.approx(0.2)

    def test_unlabelled_transcript_never_alternates(self):
        transcript = "\n".join(
            [
                "Deadlines tight hain.",
                "Team workload badh gaya hai.",
                "Priority setting needs work.",
                "Backlog kaafi lamba ho gaya.",
            ]
        )
        result = _parse(transcript)
        # Old logic would have produced HR, Employee, HR, Employee.
        assert all(t == "unknown" for t in _types(result))

    def test_unlabelled_text_kept_verbatim(self):
        raw = "Note: standup moved to 10:30\nDeadlines tight hain."
        result = _parse(raw, employee_name="")
        assert result["hasExplicitLabels"] is False
        assert [l["text"] for l in result["lines"]] == raw.splitlines()


# ---------------------------------------------------------------------------
# 4. EDGE CASES
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_lines_are_ignored(self):
        transcript = "\n\nHR: hello\n\n\nHarshit: hi there\n\n"
        result = _parse(transcript)
        assert len(result["lines"]) == 2
        assert _types(result) == ["hr", "employee"]

    def test_empty_transcript(self):
        result = _parse("")
        assert result == {"lines": [], "hasExplicitLabels": False}

    def test_whitespace_only_transcript(self):
        result = _parse("   \n\t\n  ")
        assert result == {"lines": [], "hasExplicitLabels": False}

    def test_colon_inside_dialogue_is_preserved(self):
        result = _parse("Harshit: Standup is at 10:30, okay?")
        assert len(result["lines"]) == 1
        assert result["lines"][0]["speakerType"] == "employee"
        assert result["lines"][0]["text"] == "Standup is at 10:30, okay?"

    def test_time_prefixed_line_is_not_a_speaker_label(self):
        result = _parse("10:30 standup happened today", employee_name=EMPLOYEE_NAME)
        assert result["hasExplicitLabels"] is False

    def test_crlf_line_endings_supported(self):
        result = _parse("HR: hello\r\nHarshit: hi\r\n")
        assert _types(result) == ["hr", "employee"]

    def test_timestamps_monotonic_within_duration(self):
        result = _parse(REGRESSION_TRANSCRIPT, duration=120)
        times = [line["time"] for line in result["lines"]]
        assert times == sorted(times)
        assert 0 <= times[0] and times[-1] < 120

    def test_very_long_transcript_capped_without_crash(self):
        transcript = "\n".join("HR: turn %d" % i for i in range(1, 601))
        result = _parse(transcript)
        assert len(result["lines"]) <= 500


# ---------------------------------------------------------------------------
# 5. WORKSPACE INTEGRATION (SOURCE INSPECTION)
# ---------------------------------------------------------------------------


class TestWorkspaceIntegration:
    def test_old_alternating_speaker_logic_removed(self):
        src = _read(_WORKSPACE_HTML)
        assert "(i % 2 === 0)" not in src
        assert "% 2" not in src

    def test_workspace_delegates_to_priority_parser(self):
        src = _read(_WORKSPACE_HTML)
        assert "VooVrTranscript.parse(state.transcriptText" in src

    def test_parser_loaded_before_workspace_init(self):
        src = _read(_WORKSPACE_HTML)
        assert src.find("transcript-parser.js") != -1
        assert src.find("transcript-parser.js") < src.find("voovrInitWorkspace")

    def test_parser_itself_never_alternates(self):
        src = _read(_PARSER_JS)
        assert "% 2 ===" not in src and "%2 ===" not in src
        assert "explicit_label" in src
        assert "semantic" in src

    def test_export_preserves_raw_transcript_text(self):
        src = _read(_WORKSPACE_HTML)
        assert "new Blob([state.transcriptText]" in src

    def test_moments_still_map_via_raw_transcript_index(self):
        src = _read(_WORKSPACE_HTML)
        assert "state.transcriptText.toLowerCase()" in src
        assert "text.indexOf(String(quote).toLowerCase())" in src

    def test_search_filter_intact_on_dialogue_text(self):
        src = _read(_WORKSPACE_HTML)
        assert "getElementById('wsSearch')" in src
        assert "l.text.toLowerCase().indexOf(state.query.toLowerCase())" in src

    def test_speaker_dropdown_filters_by_resolved_type(self):
        src = _read(_WORKSPACE_HTML)
        assert "l.speakerType !== state.speaker.toLowerCase()" in src

    def test_copy_line_uses_parsed_dialogue_text(self):
        src = _read(_WORKSPACE_HTML)
        assert "state.lines[idx].text" in src

    def test_disclaimer_reflects_label_source(self):
        src = _read(_WORKSPACE_HTML)
        assert 'id="wsDisclaimer"' in src
        assert "Speaker labels follow the labels present in the transcript." in src

    def test_employee_display_name_used_for_employee_turns(self):
        src = _read(_WORKSPACE_HTML)
        # Non-HR speakers render the resolved speaker name from the parser,
        # which is the real employee record name when known.
        assert "esc(speakerName)" in src

