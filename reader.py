from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

BASE_DIR = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
    ".flv",
    ".wmv",
    ".mpeg",
    ".mpg",
}
TRANSCRIPTIONS_FILE = BASE_DIR / "documents" / "transcriptions.json"
CONTEXT_DIR = BASE_DIR / "context"
CTA_PATTERNS = {
    "comment": ("comment", "comenta", "comente", "deixa aqui", "deixe aqui"),
    "follow": ("follow", "segue", "sigam", "siga"),
    "save": ("save", "salva", "salve", "compartilha", "share"),
    "link": ("link in bio", "link na bio", "link da bio", "link da bill"),
}
GREETING_PREFIXES = (
    "ola",
    "olá",
    "oi",
    "oie",
    "e ai",
    "e aí",
    "fala",
    "salve",
    "hello",
    "hey",
    "hi",
    "hi guys",
    "hey guys",
)
META_INTRO_PREFIXES = (
    "hoje vamos",
    "hoje eu vou",
    "hoje vou",
    "nesse video",
    "neste video",
    "no video de hoje",
    "today we",
    "today i",
    "in this video",
    "vamos falar sobre",
)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return " ".join(without_accents.casefold().split())


def _infer_creator(
    video: str,
    source_path: str,
    explicit_creator: str | None = None,
) -> str:
    creator = (explicit_creator or "").strip()
    if creator:
        return creator

    if source_path:
        source_obj = Path(source_path)
        if len(source_obj.parts) > 1:
            return source_obj.parts[0]
        if source_obj.stem:
            return source_obj.stem

    video_obj = Path(video)
    if len(video_obj.parts) > 1:
        return video_obj.parts[0]
    if video_obj.stem:
        return video_obj.stem
    return "unknown"


def _record_matches_query(query_normalized: str, record: dict[str, str]) -> bool:
    creator = _normalize_text(record["creator"])
    video = _normalize_text(record["video"])
    source_path = _normalize_text(record["source_path"])
    transcription = _normalize_text(record["transcription"])
    return (
        query_normalized in creator
        or query_normalized in video
        or query_normalized in source_path
        or query_normalized in transcription
    )


def _record_score(query_normalized: str, record: dict[str, str]) -> int:
    tokens = [token for token in query_normalized.split(" ") if token]
    if not tokens:
        return 0

    creator = _normalize_text(record["creator"])
    video = _normalize_text(record["video"])
    source_path = _normalize_text(record["source_path"])
    transcription = _normalize_text(record["transcription"])

    score = 0
    for token in tokens:
        if token in creator:
            score += 4
        if token in video:
            score += 3
        if token in source_path:
            score += 2
        if token in transcription:
            score += 1
    return score


def _coerce_record(
    item: object,
    explicit_creator: str | None = None,
) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None

    video = str(item.get("video", "")).strip()
    transcription = str(item.get("transcription", "")).strip()
    source_path = str(item.get("source_path", video)).strip()
    creator = _infer_creator(
        video=video,
        source_path=source_path,
        explicit_creator=explicit_creator or str(item.get("creator", "")).strip(),
    )

    if not source_path:
        source_path = video

    if not video and source_path:
        video = Path(source_path).name

    if not video or not transcription:
        return None

    return {
        "video": video,
        "creator": creator,
        "source_path": source_path,
        "transcription": transcription,
    }


def _load_transcriptions() -> list[dict[str, str]]:
    if not TRANSCRIPTIONS_FILE.exists():
        return []

    try:
        data = json.loads(TRANSCRIPTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    records: list[dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            record = _coerce_record(item)
            if record:
                records.append(record)
        return records

    if not isinstance(data, dict):
        return []

    creators = data.get("creators")
    if isinstance(creators, list):
        for creator_entry in creators:
            if not isinstance(creator_entry, dict):
                continue
            creator_name = str(creator_entry.get("creator", "")).strip()
            videos = creator_entry.get("videos", [])
            if not isinstance(videos, list):
                continue
            for item in videos:
                record = _coerce_record(item, explicit_creator=creator_name)
                if record:
                    records.append(record)
        return records

    if isinstance(creators, dict):
        for creator_name, creator_entry in creators.items():
            videos = []
            if isinstance(creator_entry, list):
                videos = creator_entry
            elif isinstance(creator_entry, dict):
                videos = creator_entry.get("videos", [])
            if not isinstance(videos, list):
                continue
            for item in videos:
                record = _coerce_record(item, explicit_creator=str(creator_name))
                if record:
                    records.append(record)

    return records


def list_available_creators() -> list[str]:
    records = _load_transcriptions()
    creators = {record["creator"] for record in records}
    return sorted(creators)


def _group_records(
    records: list[dict[str, str]],
    *,
    include_transcriptions: bool,
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for record in sorted(records, key=lambda item: (item["creator"], item["video"])):
        grouped.setdefault(record["creator"], [])
        video_payload: dict[str, str] = {
            "video": record["video"],
            "source_path": record["source_path"],
        }
        if include_transcriptions:
            video_payload["transcription"] = record["transcription"]
        grouped[record["creator"]].append(video_payload)

    return [
        {
            "creator": creator,
            "video_count": len(videos),
            "videos": videos,
        }
        for creator, videos in grouped.items()
    ]


def _build_payload(
    records: list[dict[str, str]],
    *,
    include_transcriptions: bool,
    query: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    creators = _group_records(
        records,
        include_transcriptions=include_transcriptions,
    )
    payload: dict[str, object] = {
        "query": query or None,
        "summary": {
            "total_creators": len(creators),
            "total_videos": len(records),
        },
        "creators": creators,
    }
    if note:
        payload["note"] = note
    return payload


def get_transcription_library(query: str | None = None) -> str:
    query_raw = str(query or "").strip()
    query_normalized = _normalize_text(query_raw)

    records = _load_transcriptions()
    if not records:
        return (
            "No transcriptions found. Run reader.py first to generate "
            "documents/transcriptions.json."
        )

    matches = records
    note: str | None = None
    if query_normalized:
        matches = [
            record
            for record in records
            if _record_matches_query(query_normalized, record)
        ]
        if not matches:
            note = f"No matches found for '{query_raw}'. Returning the full library."
            matches = records

    payload = _build_payload(
        matches,
        include_transcriptions=False,
        query=query_raw or None,
        note=note,
    )
    payload["available_creators"] = list_available_creators()

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _match_creator_names(query_normalized: str, creators: list[str]) -> list[str]:
    direct_matches = [
        creator for creator in creators if query_normalized in _normalize_text(creator)
    ]
    if direct_matches:
        return sorted(direct_matches)

    tokens = [token for token in query_normalized.split(" ") if token]
    ranked_matches = sorted(
        (
            (
                creator,
                sum(1 for token in tokens if token in _normalize_text(creator)),
            )
            for creator in creators
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return [creator for creator, score in ranked_matches if score > 0]


def get_creator_transcriptions(creator_name: str | None = None) -> str:
    query_raw = str(creator_name or "").strip()
    query = _normalize_text(query_raw)
    if not query:
        return "Provide a creator name."

    records = _load_transcriptions()
    if not records:
        return "No transcriptions found. Run reader.py first to generate documents/transcriptions.json."

    creator_matches = _match_creator_names(query, list_available_creators())
    matches = [
        record for record in records if record["creator"] in creator_matches
    ] if creator_matches else []
    note: str | None = None

    if not matches:
        ranked_matches = sorted(
            (
                (record, _record_score(query, record))
                for record in records
                if _record_score(query, record) > 0
            ),
            key=lambda item: item[1],
            reverse=True,
        )

        if ranked_matches:
            matches = [record for record, _ in ranked_matches[:5]]
            note = (
                f"No direct creator match for '{query_raw}'. "
                "Returning closest transcript matches."
            )
        else:
            matches = records[:5]
            note = (
                f"No direct matches for '{query_raw}'. "
                "Returning available transcripts so you can continue."
            )

    payload = _build_payload(
        matches,
        include_transcriptions=True,
        query=query_raw,
        note=note,
    )
    payload["available_creators"] = list_available_creators()

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _extract_sentences(text: str) -> list[str]:
    collapsed = " ".join(text.split())
    parts = re.split(r"(?<=[.!?])\s+", collapsed)
    return [part.strip() for part in parts if part.strip()]


def _excerpt_words(text: str, words: int = 18) -> str:
    tokens = text.split()
    if len(tokens) <= words:
        return " ".join(tokens)
    return " ".join(tokens[:words]) + "..."


def _extract_hook(text: str) -> str:
    sentences = _extract_sentences(text)
    if sentences:
        return sentences[0]
    return _excerpt_words(text)


def _extract_closing(text: str) -> str:
    sentences = _extract_sentences(text)
    if sentences:
        return sentences[-1]
    tokens = text.split()
    if len(tokens) <= 18:
        return " ".join(tokens)
    return "..." + " ".join(tokens[-18:])


def _classify_hook(hook: str) -> str:
    normalized = _normalize_text(hook)
    if "?" in hook:
        return "question"
    if re.match(r"^(\d+[\.\)]|top \d+|os \d+|these \d+)", normalized):
        return "listicle"
    if any(
        normalized.startswith(prefix)
        for prefix in (
            "como ",
            "how ",
            "how to ",
            "if your ",
            "if you ",
            "se eu ",
            "a primeira coisa",
            "basta ",
            "essa biblioteca",
            "essa ferramenta",
            "esse projeto",
            "esses sao",
            "projetos uteis",
        )
    ):
        return "tutorial_or_demo"
    return "direct_statement"


def _find_cta_categories(text: str) -> list[str]:
    normalized = _normalize_text(text)
    return [
        category
        for category, phrases in CTA_PATTERNS.items()
        if any(phrase in normalized for phrase in phrases)
    ]


def _starts_with_any_prefix(text: str, prefixes: tuple[str, ...]) -> bool:
    normalized = _normalize_text(text)
    return any(normalized.startswith(prefix) for prefix in prefixes)


def _has_greeting_opening(text: str) -> bool:
    return _starts_with_any_prefix(text, GREETING_PREFIXES)


def _has_meta_intro_opening(text: str) -> bool:
    return _starts_with_any_prefix(text, META_INTRO_PREFIXES)


def _build_recipe_guidelines(
    dominant_hook: str,
    average_word_count: float,
    cta_ratio: float,
) -> list[str]:
    guidelines: list[str] = []

    if dominant_hook == "question":
        guidelines.append("Open with a direct question that creates curiosity.")
    elif dominant_hook == "listicle":
        guidelines.append("Lead with a ranked list, count, or strong numbered promise.")
    elif dominant_hook == "tutorial_or_demo":
        guidelines.append("Open with a practical use case, then move into the demo fast.")
    else:
        guidelines.append("Lead with a bold statement or surprising claim in the first line.")

    if average_word_count < 90:
        guidelines.append("Keep the body short and fast, with one main idea and quick payoff.")
    elif average_word_count < 160:
        guidelines.append("Use 2 to 3 compact beats in the body before the conclusion.")
    else:
        guidelines.append("Use a denser explanation, but keep each beat concrete and visual.")

    if cta_ratio >= 0.5:
        guidelines.append("Close with a visible CTA such as comment, follow, save, or link.")
    else:
        guidelines.append("Close with a concise takeaway, then add a light CTA if needed.")

    return guidelines


def _build_opening_style_summary(
    *,
    total_videos: int,
    greeting_openings: int,
    meta_intro_openings: int,
    cold_open_openings: int,
) -> dict[str, object]:
    cold_open_ratio = round(
        cold_open_openings / total_videos,
        2,
    ) if total_videos else 0.0
    observed_traits: list[str] = []
    hard_constraints: list[str] = []

    if greeting_openings == 0:
        observed_traits.append(
            "Nenhuma abertura com saudação foi detectada nas amostras analisadas."
        )
        hard_constraints.append(
            "Nao comece com saudacoes como 'Ola, pessoal', 'Oi, gente', 'Hi guys' ou equivalentes."
        )
    else:
        observed_traits.append(
            f"{greeting_openings} de {total_videos} aberturas usam saudacao."
        )

    if meta_intro_openings == 0:
        observed_traits.append(
            "Nao aparecem aberturas no formato 'hoje vamos falar sobre...' nas amostras analisadas."
        )
        hard_constraints.append(
            "Nao comece com apresentacoes metalinguisticas como 'Hoje vamos falar sobre...' ou 'No video de hoje...'."
        )
    else:
        observed_traits.append(
            f"{meta_intro_openings} de {total_videos} aberturas usam introducao metalinguistica."
        )

    if cold_open_ratio >= 0.8:
        observed_traits.append(
            "A grande maioria dos videos abre direto na tese, curiosidade ou promessa."
        )
        hard_constraints.append(
            "A primeira frase deve entrar direto no ponto central do video."
        )
    elif cold_open_ratio >= 0.5:
        observed_traits.append(
            "Ha forte tendencia de abrir direto no assunto, com pouca preparacao."
        )
        hard_constraints.append(
            "Prefira entrar rapido na tese em vez de preparar demais a abertura."
        )

    hard_constraints.append(
        "Se usar a estrutura deste criador, o hook precisa soar como um cold open e nao como uma apresentacao."
    )

    return {
        "cold_open_openings": cold_open_openings,
        "cold_open_ratio": cold_open_ratio,
        "greeting_openings": greeting_openings,
        "meta_intro_openings": meta_intro_openings,
        "observed_traits": observed_traits,
        "hard_constraints": hard_constraints,
    }


def _summarize_creator_patterns(
    creator: str,
    creator_records: list[dict[str, str]],
    *,
    include_video_breakdown: bool,
) -> dict[str, object]:
    hook_types: Counter[str] = Counter()
    cta_categories: Counter[str] = Counter()
    word_counts: list[int] = []
    sample_hooks: list[str] = []
    sample_closings: list[str] = []
    video_breakdown: list[dict[str, object]] = []
    videos_with_cta = 0
    greeting_openings = 0
    meta_intro_openings = 0
    cold_open_openings = 0

    for record in creator_records:
        hook = _extract_hook(record["transcription"])
        closing = _extract_closing(record["transcription"])
        hook_type = _classify_hook(hook)
        ctas = _find_cta_categories(record["transcription"])
        word_count = len(record["transcription"].split())
        has_greeting = _has_greeting_opening(hook)
        has_meta_intro = _has_meta_intro_opening(hook)

        hook_types[hook_type] += 1
        cta_categories.update(ctas)
        word_counts.append(word_count)
        if ctas:
            videos_with_cta += 1
        if has_greeting:
            greeting_openings += 1
        if has_meta_intro:
            meta_intro_openings += 1
        if not has_greeting and not has_meta_intro:
            cold_open_openings += 1

        if len(sample_hooks) < 3:
            sample_hooks.append(hook)
        if len(sample_closings) < 3:
            sample_closings.append(closing)

        if include_video_breakdown:
            video_breakdown.append(
                {
                    "video": record["video"],
                    "hook": hook,
                    "hook_type": hook_type,
                    "closing": closing,
                    "word_count": word_count,
                    "cta_categories": ctas,
                    "has_greeting_opening": has_greeting,
                    "has_meta_intro_opening": has_meta_intro,
                }
            )

    average_word_count = (
        round(sum(word_counts) / len(word_counts), 1) if word_counts else 0.0
    )
    cta_ratio = videos_with_cta / len(creator_records) if creator_records else 0.0
    dominant_hook = hook_types.most_common(1)[0][0] if hook_types else "direct_statement"

    payload: dict[str, object] = {
        "creator": creator,
        "video_count": len(creator_records),
        "average_word_count": average_word_count,
        "dominant_hook_type": dominant_hook,
        "hook_type_distribution": dict(hook_types),
        "videos_with_explicit_cta": videos_with_cta,
        "cta_category_distribution": dict(cta_categories),
        "sample_hooks": sample_hooks,
        "sample_closings": sample_closings,
        "opening_style": _build_opening_style_summary(
            total_videos=len(creator_records),
            greeting_openings=greeting_openings,
            meta_intro_openings=meta_intro_openings,
            cold_open_openings=cold_open_openings,
        ),
        "recipe_guidelines": _build_recipe_guidelines(
            dominant_hook,
            average_word_count,
            cta_ratio,
        ),
    }
    if include_video_breakdown:
        payload["video_breakdown"] = video_breakdown
    return payload


def _summarize_shared_creator_traits(
    patterns: list[dict[str, object]],
) -> dict[str, object]:
    if not patterns:
        return {
            "creators_analyzed": 0,
            "shared_traits": [],
            "default_constraints": [],
        }

    total_creators = len(patterns)
    no_greeting_creators = sum(
        1
        for pattern in patterns
        if isinstance(pattern.get("opening_style"), dict)
        and pattern["opening_style"].get("greeting_openings") == 0
    )
    no_meta_intro_creators = sum(
        1
        for pattern in patterns
        if isinstance(pattern.get("opening_style"), dict)
        and pattern["opening_style"].get("meta_intro_openings") == 0
    )
    high_cold_open_creators = sum(
        1
        for pattern in patterns
        if isinstance(pattern.get("opening_style"), dict)
        and float(pattern["opening_style"].get("cold_open_ratio", 0.0)) >= 0.8
    )

    shared_traits: list[str] = []
    default_constraints: list[str] = []

    if no_greeting_creators == total_creators:
        shared_traits.append(
            "Todos os criadores analisados abrem sem saudacoes explicitas."
        )
        default_constraints.append(
            "Nao use 'Ola, pessoal', 'Oi, gente', 'Hi guys' ou aberturas equivalentes."
        )
    if no_meta_intro_creators == total_creators:
        shared_traits.append(
            "Nenhum criador analisado abre com 'hoje vamos falar sobre...' ou formula semelhante."
        )
        default_constraints.append(
            "Nao comece com 'Hoje vamos falar sobre...' ou 'No video de hoje...'."
        )
    if high_cold_open_creators >= max(1, total_creators - 1):
        shared_traits.append(
            "A estrutura dominante entre os criadores e de cold open direto no assunto."
        )
        default_constraints.append(
            "A primeira frase deve comecar ja na tese, curiosidade, contraste ou promessa."
        )

    if not default_constraints:
        default_constraints.append(
            "Extraia as restricoes de abertura a partir do criador mais relevante."
        )

    return {
        "creators_analyzed": total_creators,
        "shared_traits": shared_traits,
        "default_constraints": default_constraints,
    }


def get_creator_patterns(creator_name: str | None = None) -> str:
    query_raw = str(creator_name or "").strip()
    query = _normalize_text(query_raw)

    records = _load_transcriptions()
    if not records:
        return "No transcriptions found. Run reader.py first to generate documents/transcriptions.json."

    all_creators = list_available_creators()
    creator_names = all_creators
    note: str | None = None

    if query:
        creator_names = _match_creator_names(query, all_creators)
        if not creator_names:
            note = (
                f"No creator match for '{query_raw}'. Returning pattern summaries for "
                "all indexed creators."
            )
            creator_names = all_creators

    patterns = [
        _summarize_creator_patterns(
            creator,
            [record for record in records if record["creator"] == creator],
            include_video_breakdown=bool(query),
        )
        for creator in creator_names
    ]

    payload: dict[str, object] = {
        "query": query_raw or None,
        "shared_patterns": _summarize_shared_creator_traits(patterns),
        "creator_patterns": patterns,
    }
    if note:
        payload["note"] = note

    return json.dumps(payload, ensure_ascii=False, indent=2)


def extract_audio(video_path: Path, audio_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(audio_path),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required but was not found in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr_tail = "\n".join(exc.stderr.splitlines()[-10:])
        raise RuntimeError(
            f"ffmpeg failed for '{video_path.name}'.\n{stderr_tail}"
        ) from exc


def transcribe_audio(client: Groq, audio_path: Path) -> str:
    with audio_path.open("rb") as audio_file:
        response = client.audio.transcriptions.create(
            file=(audio_path.name, audio_file.read()),
            model="whisper-large-v3",
            response_format="json",
            temperature=0,
        )

    text = getattr(response, "text", None)
    if text is not None:
        return text

    if isinstance(response, dict):
        return str(response.get("text", ""))

    return str(response)


def iter_video_files(input_dir: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in input_dir.rglob("*")
            if (
                path.is_file()
                and path.suffix.lower() in VIDEO_EXTENSIONS
                and len(path.relative_to(input_dir).parts) > 1
            )
        ]
    )


def main() -> None:
    load_dotenv(BASE_DIR / ".env", override=True)

    input_dir = CONTEXT_DIR
    output_dir = TRANSCRIPTIONS_FILE.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        raise SystemExit(f"Input folder not found: {input_dir.resolve()}")

    videos = iter_video_files(input_dir)
    if not videos:
        print("No video files found inside subfolders of 'context/'.")
        return

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise SystemExit("Missing GROQ_API_KEY in environment or .env file.")

    client = Groq(api_key=api_key)
    results: list[dict[str, str]] = []

    print(f"Found {len(videos)} video(s). Starting transcription...")
    with tempfile.TemporaryDirectory(prefix="audio_extract_") as temp_dir:
        temp_path = Path(temp_dir)

        for index, video in enumerate(videos, start=1):
            relative_path = video.relative_to(input_dir)
            creator = _infer_creator(video.name, str(relative_path))
            print(
                f"[{index}/{len(videos)}] Processing {relative_path} "
                f"(creator: {creator})"
            )
            audio_path = temp_path / f"{index:04d}_{video.stem}.wav"

            extract_audio(video, audio_path)
            transcript = transcribe_audio(client, audio_path).strip()
            results.append(
                {
                    "video": video.name,
                    "creator": creator,
                    "source_path": str(relative_path),
                    "transcription": transcript,
                }
            )

    output_json = output_dir / "transcriptions.json"
    output_payload = _build_payload(results, include_transcriptions=True)
    output_json.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    creators_count = len(output_payload["creators"])
    print(
        f"Saved transcriptions: {output_json} "
        f"({len(results)} video(s) across {creators_count} creator(s))"
    )


if __name__ == "__main__":
    main()
