"""
server/gemini/client.py
=======================
Gemini クライアント本実装 (テキストベース版)。

処理フロー:
  edge側でウェイクワード検知 -> 発話録音 -> Google Cloud STT でテキスト化
  -> GeminiRequest(text_input=...) としてサーバーに送信
  -> process_text_request() で generate_content(text) を呼ぶ
       -> Function Call があれば edge に転送して実行を待つ
       -> 結果をマルチターンでGeminiに返し、最終テキストを得る
       -> テキストから <emotion>タグ を抜き出して GeminiResponse を生成

(旧: 音声PCMをWAV化してGeminiに直接送信する方式だったが、
 STTでテキスト化してから送る方式に変更し、レイテンシを大幅に削減した)

会話履歴は ConversationHistory で管理し、古いターンは MAX_TURNS で間引く。
function_call の実行は edge 側 (FunctionCall -> FunctionResult) で行われるため、
ここでは「実行待ちのFuture」を保持し、サーバー側 (app.py) から
receive_function_result() で解決してもらう。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Dict, List, Optional

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

from shared.protocol.messages import (
    FunctionCall, FunctionResult, GeminiResponse, EmotionState,
)
from shared.protocol.config import cfg
from shared.functions.definitions import get_tools_schema, get_function_names
from server.gemini.prompts import build_system_prompt

log = logging.getLogger(__name__)

ResponseCallback = Callable[[GeminiResponse], Awaitable[None]]
FunctionCallSender = Callable[[FunctionCall], Awaitable[None]]

MAX_TURNS = 20                                            # 会話履歴の保持ターン数
FUNCTION_CALL_TIMEOUT_SEC = 8.0
MAX_FUNCTION_CALL_ROUNDS = 4                              # 無限ループ防止

_EMOTION_TAG_RE = re.compile(r"<emotion>\s*(\w+)\s*</emotion>", re.IGNORECASE)
_VALID_EMOTIONS = {e.value for e in EmotionState}


def _parse_text_and_emotion(raw_text: str) -> tuple[str, EmotionState]:
    """
    Geminiの応答テキストから <emotion>xxx</emotion> を抜き出し、
    残りのテキストと EmotionState のペアを返す。
    タグが無い/不正な値の場合は NEUTRAL とする。
    """
    if not raw_text:
        return "", EmotionState.NEUTRAL

    m = _EMOTION_TAG_RE.search(raw_text)
    emotion = EmotionState.NEUTRAL
    if m:
        tag = m.group(1).lower()
        if tag in _VALID_EMOTIONS:
            emotion = EmotionState(tag)
        clean_text = _EMOTION_TAG_RE.sub("", raw_text).strip()
    else:
        clean_text = raw_text.strip()

    return clean_text, emotion


class ConversationHistory:
    """
    Gemini の `contents` 形式 ({"role": ..., "parts": [...]}) で
    マルチターン履歴を保持する。MAX_TURNS を超えたら古いターンから削除する。
    """

    def __init__(self, max_turns: int = MAX_TURNS) -> None:
        self._max_turns = max_turns
        self._turns: List[dict] = []  # 各要素 = 1ターン分の contents エントリ群

    def add_user_text_turn(self, text: str) -> None:
        self._turns.append({"role": "user", "parts": [{"text": text}]})
        self._trim()

    def add_model_turn(self, parts: list) -> None:
        self._turns.append({"role": "model", "parts": parts})
        self._trim()

    def add_function_response_turn(self, parts: list) -> None:
        self._turns.append({"role": "function", "parts": parts})
        self._trim()

    def _trim(self) -> None:
        # ユーザー発話単位(1往復=user+model[+function...])で間引きたいが、
        # 簡易的に「ターン数上限」で古いものから削除する
        while len(self._turns) > self._max_turns:
            self._turns.pop(0)

    def as_contents(self) -> List[dict]:
        return list(self._turns)

    def clear(self) -> None:
        self._turns.clear()


class GeminiClient:

    def __init__(self) -> None:
        self._response_callback: Optional[ResponseCallback] = None
        self._function_call_sender: Optional[FunctionCallSender] = None
        self._pending_fc: Dict[str, asyncio.Future] = {}
        self._tools = get_tools_schema()
        self._function_names = set(get_function_names())
        self._history = ConversationHistory()
        self._model: Optional["genai.GenerativeModel"] = None
        self._running = False
        self._call_counter = 0

    async def start(self) -> None:
        self._running = True
        api_key = cfg.secrets.gemini_api_key
        if api_key is None:
            log.warning("[gemini] GEMINI_API_KEY not set — running in stub mode")
            return

        genai.configure(api_key=api_key.get_secret_value())

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        self._model = genai.GenerativeModel(
            model_name=cfg.gemini.model,
            system_instruction=build_system_prompt(),
            tools=self._tools,
            safety_settings=safety_settings,
            generation_config=genai.GenerationConfig(
                max_output_tokens=cfg.gemini.max_tokens,
                temperature=cfg.gemini.temperature,
            ),
        )
        log.info("[gemini] client ready (model=%s)", cfg.gemini.model)

    async def stop(self) -> None:
        self._running = False
        for fut in self._pending_fc.values():
            if not fut.done():
                fut.cancel()
        self._pending_fc.clear()

    def set_response_callback(self, cb: Optional[ResponseCallback]) -> None:
        self._response_callback = cb

    def set_function_call_sender(self, sender: Optional[FunctionCallSender]) -> None:
        """edge へ FunctionCall を送信する関数を登録する (app.py から呼ばれる)"""
        self._function_call_sender = sender

    # ── テキストリクエスト処理 ──────────────────────────────
    async def process_text_request(self, session_id: str, text: str) -> None:
        """
        edge側でGoogle Cloud STTにより文字起こし済みのテキストを受け取り、
        Geminiに送って応答を生成する。
        """
        if not text or not text.strip():
            log.info("[gemini] empty text — ignored (session=%s)", session_id)
            return

        log.info("[gemini] process_text_request: session=%s text='%s'", session_id, text[:80])

        if self._model is None:
            log.warning("[gemini] model not ready — emitting stub response")
            await self._emit(GeminiResponse(
                session_id=session_id,
                text="ごめんなさい、まだ準備中みたいです。",
                emotion=EmotionState.SAD,
            ))
            return

        try:
            response = await self._run_conversation_turn(session_id, text)
            await self._emit(response)
        except Exception as exc:
            log.error("[gemini] turn processing error: %s", exc, exc_info=True)
            await self._emit(GeminiResponse(
                session_id=session_id,
                text="うーん、ちょっと考えがまとまらなかった。",
                emotion=EmotionState.SAD,
            ))

    # ── Gemini呼び出し本体 ──────────────────────────────────
    async def _run_conversation_turn(self, session_id: str, text: str) -> GeminiResponse:
        self._history.add_user_text_turn(text)
        contents = self._history.as_contents()

        loop = asyncio.get_running_loop()
        gem_response = await loop.run_in_executor(
            None, lambda: self._model.generate_content(contents)
        )

        rounds = 0
        while rounds < MAX_FUNCTION_CALL_ROUNDS:
            function_calls = self._extract_function_calls(gem_response)
            if not function_calls:
                break

            model_parts = self._extract_model_parts(gem_response)
            self._history.add_model_turn(model_parts)

            fc_response_parts = []
            for fc in function_calls:
                result = await self._dispatch_function_call(fc)
                fc_response_parts.append({
                    "function_response": {
                        "name": fc.name,
                        "response": {"result": result},
                    }
                })
            self._history.add_function_response_turn(fc_response_parts)

            contents = self._history.as_contents()
            gem_response = await loop.run_in_executor(
                None, lambda: self._model.generate_content(contents)
            )
            rounds += 1

        final_text = self._extract_text(gem_response)
        self._history.add_model_turn([{"text": final_text}] if final_text else [])

        clean_text, emotion = _parse_text_and_emotion(final_text)
        tokens_used = 0
        try:
            tokens_used = gem_response.usage_metadata.total_token_count
        except Exception:
            pass

        return GeminiResponse(
            session_id=session_id,
            text=clean_text or "うまく聞き取れなかった、もう一度お願いできる？",
            emotion=emotion,
            tokens_used=tokens_used,
        )

    def _extract_text(self, response) -> str:
        try:
            return response.text or ""
        except Exception:
            parts_text = []
            try:
                for cand in response.candidates:
                    for part in cand.content.parts:
                        if hasattr(part, "text") and part.text:
                            parts_text.append(part.text)
            except Exception:
                pass
            return "".join(parts_text)

    def _extract_function_calls(self, response) -> List[FunctionCall]:
        calls: List[FunctionCall] = []
        try:
            for cand in response.candidates:
                for part in cand.content.parts:
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name:
                        self._call_counter += 1
                        calls.append(FunctionCall(
                            name=fc.name,
                            call_id=f"fc_{self._call_counter:05d}",
                            arguments=dict(fc.args) if fc.args else {},
                        ))
        except Exception as exc:
            log.debug("[gemini] no function calls extracted: %s", exc)
        return calls

    def _extract_model_parts(self, response) -> list:
        parts = []
        try:
            for cand in response.candidates:
                for part in cand.content.parts:
                    if hasattr(part, "text") and part.text:
                        parts.append({"text": part.text})
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name:
                        parts.append({
                            "function_call": {
                                "name": fc.name,
                                "args": dict(fc.args) if fc.args else {},
                            }
                        })
        except Exception:
            pass
        return parts or [{"text": ""}]

    async def _dispatch_function_call(self, fc: FunctionCall) -> dict:
        """
        FunctionCall を edge に送信し、FunctionResult が返るまで待つ。
        edge との接続がない / タイムアウトの場合はエラー結果を返す。
        """
        if fc.name not in self._function_names:
            log.warning("[gemini] unknown function requested: %s", fc.name)
            return {"error": f"unknown function: {fc.name}"}

        if self._function_call_sender is None:
            log.warning("[gemini] no function_call_sender registered — skipping %s", fc.name)
            return {"error": "edge not connected"}

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_fc[fc.call_id] = fut

        try:
            await self._function_call_sender(fc)
            result = await asyncio.wait_for(fut, timeout=FUNCTION_CALL_TIMEOUT_SEC)
            if isinstance(result, FunctionResult):
                if result.error:
                    return {"error": result.error}
                return {"ok": True, "result": result.result}
            return {"ok": True}
        except asyncio.TimeoutError:
            log.warning("[gemini] function call timeout: %s", fc.name)
            return {"error": "timeout"}
        finally:
            self._pending_fc.pop(fc.call_id, None)

    async def receive_function_result(self, result: FunctionResult) -> None:
        fut = self._pending_fc.get(result.call_id)
        if fut and not fut.done():
            fut.set_result(result)
        else:
            log.debug("[gemini] unexpected function result: %s", result.call_id)

    async def _emit(self, response: GeminiResponse) -> None:
        if self._response_callback:
            try:
                await self._response_callback(response)
            except Exception as exc:
                log.error("[gemini] response callback error: %s", exc)
