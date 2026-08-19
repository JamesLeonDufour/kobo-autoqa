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
    # The request puts the whole regional code in `language` and has no
    # `locale` field at all. The stored *result* comes back split as
    # {"language": "fr", "locale": "fr-FR"}, which is not the shape to send --
    # sending it earns `400 Invalid payload`, as does the bare region subtag.
    fails += not ok("the whole regional code goes in `language`",
                    body[XPATH]["automatic_google_transcription"]
                    == {"language": "fr-FR"}, body[XPATH])
    fails += not ok("no locale field is ever sent",
                    "locale" not in body[XPATH]["automatic_google_transcription"])
    fails += not ok("a bare code is sent as-is",
                    S.transcribe_body(XPATH, "fr")[XPATH]["automatic_google_transcription"]
                    == {"language": "fr"})
    fails += not ok("accepting a transcript uses the same shape",
                    S.accept_transcript_body(XPATH, "fr-FR")[XPATH]
                    ["automatic_google_transcription"]
                    == {"language": "fr-FR", "accepted": True})
    # Translation has no regional variants -- /api/v2/languages/fr/ offers the
    # single code "fr" -- so a locale there would be meaningless.
    fails += not ok("translation targets stay bare",
                    S.translate_body(XPATH, "fr-FR")[XPATH]["automatic_google_translation"]
                    == {"language": "fr"})
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
                    ctx.features.transcribe[XPATH] == "fr"
                    and ctx.features.translate[XPATH] == ["en", "es"]
                    and len(ctx.features.qual[XPATH]) == 2,
                    ctx.features.describe())
    # A recording the form does not configure gets transcription of its own.
    # Judging each recording separately is what makes a question added to a
    # form later get picked up at all: the form as a whole already looked
    # configured, so nothing ever looked at the new one.
    fails += not ok("an unconfigured recording is given one",
                    ctx.features.transcribe.get("ambient") == settings.transcript_language,
                    ctx.features.transcribe)
    fails += not ok("without disturbing the configured one",
                    ctx.features.transcribe[XPATH] == "fr", ctx.features.transcribe[XPATH])
    fails += not ok("both recordings are then processed",
                    sorted(ctx.xpaths) == ["ambient", XPATH], ctx.xpaths)

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

    # Params are append-only, so applying a question set ADDS to whatever the
    # form already had. The questions seeded on this form -- as if someone had
    # set it up in Kobo's own UI -- are still there afterwards, and nothing the
    # app can send will remove them.
    labels = [q["labels"]["_default"] for q in after.definitions[XPATH]]
    by_label = {q["labels"]["_default"]: q for q in after.definitions[XPATH]}
    fails += not ok("new questions added to the form",
                    {"Which needs are mentioned?", "How successful would you rank this",
                     "Section heading for human coders"} <= set(labels), labels)
    fails += not ok("questions already on the form survive",
                    "Seeded question 1" in labels, labels)
    fails += not ok("question hint kept",
                    by_label["Which needs are mentioned?"]["hint"]["labels"]["_default"]
                    == "List them, comma separated")
    ranked = by_label["How successful would you rank this"]
    fails += not ok("choices kept", len(ranked["choices"]) == 2)
    fails += not ok("choice hint kept",
                    ranked["choices"][0]["hint"]["labels"]["_default"]
                    == "Pick this when nothing went well", ranked["choices"][0])
    stored_defs = after.definitions[XPATH]
    answerable = set(after.answerable_qual(XPATH))
    fails += not ok("notes excluded from AI answering",
                    by_label["Section heading for human coders"]["uuid"] not in answerable)
    fails += not ok("answerable questions are asked",
                    {by_label["Which needs are mentioned?"]["uuid"],
                     by_label["How successful would you rank this"]["uuid"]} <= answerable)
    fails += not ok("every uuid asked about has a definition",
                    all(after.question_type(XPATH, u) for u in answerable))
    # Storing the base language threw away the only part that names a real
    # recognition model, and the row is append-only so it could not be undone.
    fails += not ok("the region survives being written", after.transcribe[XPATH] == "fr-FR",
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
    applied = {q["labels"]["_default"] for q in tagged}
    fails += not ok("both recordings ask the applied questions",
                    applied <= {q["labels"]["_default"] for q in both.definitions[XPATH]}
                    and applied <= {q["labels"]["_default"] for q in both.definitions[SECOND]})
    # The first recording keeps the questions it already had as well: applying
    # a set to a second recording cannot subtract from the first, and nothing
    # can subtract from either.
    fails += not ok("and the first keeps what it already had",
                    "Seeded question 1" in
                    {q["labels"]["_default"] for q in both.definitions[XPATH]})
    uu1 = {q["uuid"] for q in both.definitions[XPATH]}
    uu2 = {q["uuid"] for q in both.definitions[SECOND]}
    fails += not ok("each recording has its own question uuids", not (uu1 & uu2),
                    sorted(uu1 & uu2))
    pick = lambda feats, x, label: next(  # noqa: E731
        q for q in feats.definitions[x] if q["labels"]["_default"] == label)
    ranked_label = "How successful would you rank this"
    fails += not ok("choice uuids are per recording too",
                    not ({c["uuid"] for c in pick(both, XPATH, ranked_label)["choices"]}
                         & {c["uuid"] for c in pick(both, SECOND, ranked_label)["choices"]}))

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
    # `merge=False` cannot strip anything, because the server merges whatever
    # it is sent. Applying a set that omits a question leaves that question
    # exactly where it was -- there is no way to remove one through this API.
    edited = {q["labels"]["_default"] for q in feats.definitions[XPATH]}
    fails += not ok("questions omitted from an edit are NOT removed",
                    "Seeded question 1" in edited, sorted(edited))
    fails += not ok("everything applied is present",
                    {q["labels"]["_default"] for q in tagged} <= edited, sorted(edited))

    # re-applying the same set must not churn uuids or duplicate questions
    before = {x: {q["uuid"] for q in feats.definitions[x]} for x in (XPATH, SECOND)}
    counts = {x: len(feats.definitions[x]) for x in (XPATH, SECOND)}
    for x in (XPATH, SECOND):
        S.sync_question(client, U, S.AssetFeatures(client.list_advanced_features(U)),
                        x, questions=tagged)
    again = S.AssetFeatures(client.list_advanced_features(U))
    fails += not ok("re-apply churns no uuids",
                    all({q["uuid"] for q in again.definitions[x]} == before[x]
                        for x in (XPATH, SECOND)))
    fails += not ok("and duplicates nothing",
                    all(len(again.definitions[x]) == counts[x] for x in (XPATH, SECOND)),
                    {x: len(again.definitions[x]) for x in (XPATH, SECOND)})

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

    print("\n[what the server can actually recognise]")
    doc = {"name": "French",
           "transcription_services": {"goog": {"fr-CA": "fr-CA", "fr-FR": "fr-FR"}},
           "translation_services": {"goog": {"fr": "fr"}}}
    fails += not ok("ASR variants read from the language document",
                    S.asr_variants(doc) == ["fr-CA", "fr-FR"], S.asr_variants(doc))
    fails += not ok("no bare language among them", "fr" not in S.asr_variants(doc))
    fails += not ok("translation support detected", S.translatable(doc))
    fails += not ok("a language with no ASR reports none",
                    S.asr_variants({"transcription_services": {}}) == [])

    print("\n[changing the audio language]")
    # Params are append-only, so re-saving a recording as English leaves the
    # row holding ["fr", "en"]. Reading the first one silently kept
    # transcribing in the language the user had just changed away from.
    changed = S.AssetFeatures([{"question_xpath": XPATH,
                                "action": S.ACTION_TRANSCRIBE,
                                "params": [{"language": "fr"}, {"language": "en"}]}])
    fails += not ok("the newest audio language wins",
                    changed.transcribe[XPATH] == "en", changed.transcribe)
    fails += not ok("the superseded one is still reported",
                    changed.superseded[XPATH] == ["fr"], changed.superseded)
    once = S.AssetFeatures([{"question_xpath": XPATH, "action": S.ACTION_TRANSCRIBE,
                             "params": [{"language": "fr"}]}])
    fails += not ok("an unchanged recording reports no history",
                    once.transcribe[XPATH] == "fr" and not once.superseded)

    print("\n[a job nobody is working on]")
    # Kobo can leave a submission saying in_progress while its own queue
    # reports nothing pending. Waiting politely on that means waiting for ever,
    # which is what "it is taking too long" turned out to be.
    import time as _t  # noqa: PLC0415
    fresh = {"_versions": [{"_data": {"status": "in_progress"},
                            "_dateCreated": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}]}
    old_job = {"_versions": [{"_data": {"status": "in_progress"},
                              "_dateCreated": "2020-01-01T00:00:00Z"}]}
    done_job = {"_versions": [{"_data": {"status": "complete", "value": "x"},
                               "_dateCreated": "2020-01-01T00:00:00Z"}]}
    fails += not ok("a job that just started is left alone", not S.is_stalled(fresh))
    fails += not ok("one running far too long counts as stalled", S.is_stalled(old_job))
    fails += not ok("a finished job is never stalled", S.is_stalled(done_job) is False)
    fails += not ok("nothing pending means no stall clock",
                    S.stalled_for(done_job) == 0.0)
    fails += not ok("an unparseable date does not crash it",
                    S.stalled_for({"_versions": [{"_data": {"status": "in_progress"},
                                                  "_dateCreated": "not a date"}]}) == 0.0)

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
