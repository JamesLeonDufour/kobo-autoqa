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
                      "labels": {"_default": "1"}},
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
    fails += not ok("notes excluded from AI answering",
                    after.qual[XPATH] == [new_survey[0]["uuid"], new_survey[1]["uuid"]],
                    after.qual[XPATH])
    fails += not ok("language split on write", after.transcribe[XPATH] == "fr",
                    after.transcribe[XPATH])
    fails += not ok("re-applying replaces rather than duplicates",
                    len([r for r in after.rows
                         if r["action"] == "manual_qual"]) == 1)

    print("\n[no calls to the removed endpoints]")
    r = httpx.get(f"http://127.0.0.1:8899/api/v2/assets/{U}/advanced_submission_post/")
    fails += not ok("legacy endpoint really is 404 in this mode", r.status_code == 404)

    client.close()
    print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
