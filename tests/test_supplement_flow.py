"""Drive the pipeline against a server speaking the current kpi NLP API.

tests/mock_kobo.py must be running on :8899. The mock is switched into
`supplement` mode, where the older advanced_submission_* endpoints 404 exactly
as they do on a real current server, so this exercises dialect detection as
well as the transcribe -> translate -> qual sequence.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("KOBO_URL", "http://127.0.0.1:8899")
os.environ.setdefault("KOBO_TOKEN", "x")
os.environ.setdefault("DB_PATH", "/tmp/supplement.db")

import httpx  # noqa: E402

from app import payloads as P  # noqa: E402
from app import supplement as S  # noqa: E402
from app.common import make_client, make_store  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.store import STAGE_DONE, STAGE_NEW  # noqa: E402

U = "aMOCK1234567890abcdef"
SUB = "c55d6112-ebe4-466f-a558-f9af1f7624e2"
XPATH = "section_a/Recording_001"


def ok(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(extra) if extra else ''}")
    return bool(cond)


def main() -> int:
    fails = 0
    print("\n[language splitting]")
    fails += not ok("fr-FR splits", S.split_language("fr-FR") == ("fr", "FR"))
    fails += not ok("bare code has no locale", S.split_language("en") == ("en", ""))
    fails += not ok("underscore form splits", S.split_language("pt_BR") == ("pt", "BR"))

    print("\n[payload shape]")
    body = S.transcribe_body(XPATH, "fr-FR")
    fails += not ok("version envelope present", body.get("_version") == "20250820", body)
    fails += not ok("language and locale split in payload",
                    body[XPATH]["automatic_google_transcription"] == {"language": "fr", "locale": "FR"},
                    body[XPATH])
    fails += not ok("qual addresses one uuid",
                    S.qual_body(XPATH, "abc")[XPATH]["automatic_bedrock_qual"] == {"uuid": "abc"})

    httpx.post("http://127.0.0.1:8899/__reset")
    httpx.post("http://127.0.0.1:8899/__mode/supplement")

    store = make_store(settings)
    store.advance(U, SUB, STAGE_NEW, delay=0)  # clear anything from a previous run
    client = make_client(settings, store)
    pipe = Pipeline(settings, client, store)

    print("\n[dialect detection]")
    ctx = pipe.asset_context(U)
    fails += not ok("supplement dialect detected", ctx.dialect == P.SUPPLEMENT, ctx.dialect)
    fails += not ok("server config read back",
                    ctx.features.transcribe == {XPATH: "fr"}
                    and ctx.features.translate == {XPATH: ["en", "es"]}
                    and len(ctx.features.qual[XPATH]) == 2,
                    ctx.features.describe())
    fails += not ok("xpaths derived from server config", ctx.xpaths == [XPATH], ctx.xpaths)

    print("\n[state machine]")
    store.enqueue(U, SUB, {"source": "test"})
    store.reset(U, SUB)
    seen = []
    for _ in range(12):
        row = [r for r in store.list_jobs(limit=50)
               if r["asset_uid"] == U and r["submission_uuid"] == SUB][0]
        seen.append(row["stage"])
        if row["stage"] in (STAGE_DONE, "failed"):
            break
        pipe.process(U, SUB, row["stage"], 0)
        store.advance(U, SUB, [r for r in store.list_jobs(limit=50)
                               if r["submission_uuid"] == SUB][0]["stage"], delay=0)

    fails += not ok("reached done", seen[-1] == STAGE_DONE, " -> ".join(seen))
    row = [r for r in store.list_jobs(limit=50) if r["submission_uuid"] == SUB][0]
    fails += not ok("each pass records why it stopped",
                    bool(row["note"]) and "analysis answer" in row["note"], row["note"])
    fails += not ok("passed through every stage",
                    {"transcribe", "translate", "qual"} <= set(seen), seen)

    print("\n[results landed in Kobo]")
    sup = client.get_data_supplement(U, SUB)
    status, text = S.transcript_state(sup, XPATH)
    fails += not ok("transcript complete", status == "complete" and bool(text), (status, text))
    fails += not ok("transcript accepted", S.transcript_accepted(sup, XPATH))
    fails += not ok("both translations complete",
                    all(S.translation_state(sup, XPATH, l) == "complete" for l in ("en", "es")))
    fails += not ok("translations accepted",
                    all(S.translation_accepted(sup, XPATH, l) for l in ("en", "es")))
    fails += not ok("every qual question answered",
                    all(S.qual_state(sup, XPATH, q) == "complete"
                        for q in ctx.features.qual[XPATH]))

    print("\n[configuring from the app]")
    # A question the server has never heard of, with hints on the question and
    # a choice -- this is the case that silently did nothing before.
    new_survey = [
        {"uuid": "33333333-3333-4333-8333-333333333333", "type": "qualText",
         "labels": {"_default": "Which needs are mentioned?"},
         "hint": {"labels": {"_default": "List them, comma separated"}}},
        {"uuid": "44444444-4444-4444-8444-444444444444", "type": "qualSelectOne",
         "labels": {"_default": "How successful would you rank this"},
         "hint": {"labels": {"_default": "One being the lowest, 5 the best"}},
         "choices": [{"uuid": "55555555-5555-4555-8555-555555555555",
                      "labels": {"_default": "1"},
                      "hint": {"labels": {"_default": "Pick this when nothing went well"}}},
                     {"uuid": "66666666-6666-4666-8666-666666666666",
                      "labels": {"_default": "5"}}]},
        {"uuid": "77777777-7777-4777-8777-777777777777", "type": "qualNote",
         "labels": {"_default": "Section heading for human coders"}},
    ]
    feats = S.AssetFeatures(client.list_advanced_features(U))
    S.sync_question(client, U, feats, XPATH, transcribe_language="fr-FR",
                    translate_languages=["en"], questions=new_survey)
    after = S.AssetFeatures(client.list_advanced_features(U))

    fails += not ok("new questions stored on the server",
                    [q["labels"]["_default"] for q in after.definitions[XPATH]]
                    == ["Which needs are mentioned?", "How successful would you rank this",
                        "Section heading for human coders"],
                    [q["labels"]["_default"] for q in after.definitions[XPATH]])
    fails += not ok("question hint kept",
                    after.definitions[XPATH][0]["hint"]["labels"]["_default"]
                    == "List them, comma separated")
    fails += not ok("choices kept", len(after.definitions[XPATH][1]["choices"]) == 2)
    fails += not ok("choice hint kept",
                    after.definitions[XPATH][1]["choices"][0]["hint"]["labels"]["_default"]
                    == "Pick this when nothing went well",
                    after.definitions[XPATH][1]["choices"][0])
    # uuids are minted per recording, so compare against what was stored.
    stored_defs = after.definitions[XPATH]
    fails += not ok("notes excluded from AI answering",
                    after.qual[XPATH] == [q["uuid"] for q in stored_defs
                                          if q["type"] in S.AUTO_QUAL_TYPES],
                    [(q["type"], q["uuid"] in after.qual[XPATH]) for q in stored_defs])
    fails += not ok("language split on write", after.transcribe[XPATH] == "fr",
                    after.transcribe[XPATH])
    fails += not ok("re-applying replaces rather than duplicates",
                    len([r for r in after.rows
                         if r["action"] == "manual_qual"]) == 1)

    print("\n[several recordings at once]")
    SECOND = "ambient"
    tagged = new_survey + [
        {"uuid": "88888888-8888-4888-8888-888888888888", "type": "qualTags",
         "labels": {"_default": "Key themes raised"}},
    ]
    feats = S.AssetFeatures(client.list_advanced_features(U))
    for x in (XPATH, SECOND):
        S.sync_question(client, U, feats, x, transcribe_language="fr",
                        translate_languages=["en"], questions=tagged)
        feats = S.AssetFeatures(client.list_advanced_features(U))

    both = S.AssetFeatures(client.list_advanced_features(U))
    fails += not ok("second recording configured",
                    len(both.definitions.get(SECOND, [])) == 4,
                    len(both.definitions.get(SECOND, [])))
    fails += not ok("both recordings ask the same questions",
                    [q["labels"]["_default"] for q in both.definitions[XPATH]]
                    == [q["labels"]["_default"] for q in both.definitions[SECOND]])
    uu1 = {q["uuid"] for q in both.definitions[XPATH]}
    uu2 = {q["uuid"] for q in both.definitions[SECOND]}
    fails += not ok("each recording has its own question uuids", not (uu1 & uu2),
                    sorted(uu1 & uu2))
    fails += not ok("choice uuids are per recording too",
                    not ({c["uuid"] for c in both.definitions[XPATH][1]["choices"]}
                         & {c["uuid"] for c in both.definitions[SECOND][1]["choices"]}))

    print("\n[types the model cannot answer]")
    fails += not ok("tags excluded from auto-answering",
                    all(both.question_type(SECOND, u) != "qualTags"
                        for u in both.qual[SECOND]))
    fails += not ok("tags still defined for human coding",
                    any(q["type"] == "qualTags" for q in both.definitions[SECOND]))
    # A configuration written by hand can still list one; it must be skipped
    # rather than failing the submission forever.
    hand = S.AssetFeatures([
        {"question_xpath": "x", "action": "manual_qual",
         "params": [{"uuid": "a", "type": "qualTags", "labels": {"_default": "t"}},
                    {"uuid": "b", "type": "qualText", "labels": {"_default": "q"}}]},
        {"question_xpath": "x", "action": "automatic_bedrock_qual",
         "params": [{"uuid": "a"}, {"uuid": "b"}]},
    ])
    fails += not ok("bad hand-written config is skipped, not fatal",
                    hand.answerable_qual("x") == ["b"], hand.answerable_qual("x"))

    print("\n[applying to others must not strip their own questions]")
    own = {"uuid": "99999999-9999-4999-8999-999999999999", "type": "qualText",
           "labels": {"_default": "Only asked about the ambient recording"}}
    feats = S.AssetFeatures(client.list_advanced_features(U))
    S.sync_question(client, U, feats, SECOND, questions=tagged + [own])
    feats = S.AssetFeatures(client.list_advanced_features(U))
    # Now apply the shared set (without `own`) to both, editing XPATH.
    for x in (XPATH, SECOND):
        S.sync_question(client, U, feats, x, questions=tagged, merge=(x != XPATH))
        feats = S.AssetFeatures(client.list_advanced_features(U))
    labels = [q["labels"]["_default"] for q in feats.definitions[SECOND]]
    fails += not ok("merged recording keeps its own question",
                    "Only asked about the ambient recording" in labels, labels)
    fails += not ok("edited recording is replaced, not merged",
                    len(feats.definitions[XPATH]) == len(tagged),
                    len(feats.definitions[XPATH]))

    # re-applying the same set to both must not churn uuids
    for x in (XPATH, SECOND):
        S.sync_question(client, U, S.AssetFeatures(client.list_advanced_features(U)),
                        x, questions=tagged)
    again = S.AssetFeatures(client.list_advanced_features(U))
    fails += not ok("re-apply across recordings is stable",
                    {q["uuid"] for q in again.definitions[SECOND]} == uu2)

    print("\n[a dead action is not retried forever]")
    # Every request appends a version, so a run of failures is recorded in the
    # document itself. This is the shape that burned 30 billable translation
    # calls on a live form: the transcript came back empty, so every
    # translation 400'd, and "failed" was read as "try again".
    def failed_versions(n, error, status="failed"):
        return {"_versions": [
            {"_data": {"status": status, "error": error},
             "_dateCreated": f"2026-08-18T13:{50 + i:02d}:00Z"} for i in range(n)]}

    empty_doc = {XPATH: {
        S.ACTION_TRANSCRIBE: {"_versions": [
            {"_data": {"status": "complete", "value": ""},
             "_dateCreated": "2026-08-18T13:49:48Z",
             "_dateAccepted": "2026-08-18T13:49:51Z"}]},
        S.ACTION_TRANSLATE: {"es": failed_versions(3, "400 Empty request")},
    }}
    fails += not ok("an empty transcript is not a usable transcript",
                    S.transcript_is_empty(empty_doc, XPATH))
    fails += not ok("a non-empty one still is",
                    not S.transcript_is_empty(
                        {XPATH: {S.ACTION_TRANSCRIBE: {"_versions": [
                            {"_data": {"status": "complete", "value": "bonjour"}}]}}}, XPATH))
    fails += not ok("failure streak counted from the document",
                    S.failure_streak(empty_doc[XPATH][S.ACTION_TRANSLATE]["es"]) == 3)
    fails += not ok("the server's own error is surfaced",
                    "Empty request" in S.error_text(
                        empty_doc[XPATH][S.ACTION_TRANSLATE]["es"]))
    # Newest-first is what a live server actually returns; the ordering must
    # not change which version counts as latest.
    reversed_doc = {"_versions": list(reversed(
        empty_doc[XPATH][S.ACTION_TRANSLATE]["es"]["_versions"]))}
    fails += not ok("version order does not matter",
                    S.failure_streak(reversed_doc) == 3)
    fails += not ok("a streak ends at the first success",
                    S.failure_streak({"_versions": [
                        {"_data": {"status": "failed"}, "_dateCreated": "2026-01-01T00:00:00Z"},
                        {"_data": {"status": "complete"}, "_dateCreated": "2026-01-02T00:00:00Z"},
                    ]}) == 0)

    print("\n[translating a language into itself]")
    keep, same = S.usable_targets("fr", ["es", "fr"])
    fails += not ok("self-translation dropped", keep == ["es"] and same == ["fr"], (keep, same))
    keep, same = S.usable_targets("fr-FR", ["fr", "en-GB"])
    fails += not ok("locale variants compare on the base code",
                    keep == ["en-GB"] and same == ["fr"], (keep, same))
    keep, same = S.usable_targets("", ["fr", "es"])
    fails += not ok("no source language means nothing to drop", same == [], same)

    print("\n[no calls to the removed endpoints]")
    r = httpx.get(f"http://127.0.0.1:8899/api/v2/assets/{U}/advanced_submission_post/")
    fails += not ok("legacy endpoint really is 404 in this mode", r.status_code == 404)

    client.close()
    print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
