import asyncio
import aiohttp
import shlex
import re
from urllib.parse import urlparse
from typing import Optional, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# --- 自定义异常类 ---
class DiscogsException(Exception):
    pass

class DiscogsAuthError(DiscogsException):
    pass

class DiscogsRateLimitError(DiscogsException):
    pass

class DiscogsAPIError(DiscogsException):
    pass


@register("discogs_plugin", "RyanVaderAN", "Discogs 音乐与黑胶查询", "1.0.2")
class DiscogsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = "https://api.discogs.com"
        self.session = None 
        self._session_lock = asyncio.Lock()
        
        self._allowed_web_domains = {"www.discogs.com", "discogs.com"}
        
        self.static_headers = {
            "User-Agent": "AstrBotDiscogsPlugin/1.5.3 +https://github.com/AstrBotDevs",
            "Accept": "application/vnd.discogs.v2.discogs+json"
        }

    # 🚀 优化：抽取 token 获取逻辑，符合 DRY 原则
    def _get_token(self) -> str:
        return str(self.config.get("discogs_token") or "").strip()

    def _is_token_configured(self) -> bool:
        return bool(self._get_token())

    def _get_auth_header(self) -> Dict[str, str]:
        token = self._get_token()
        if token:
            return {"Authorization": f"Discogs token={token}"}
        return {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            async with self._session_lock:
                if self.session is None or self.session.closed:
                    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
                    self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    def _normalize_discogs_url(self, endpoint_or_url: str) -> str:
        if endpoint_or_url.startswith(("http://", "https://")):
            parsed = urlparse(endpoint_or_url)
            # 🚀 优化：显式强制小写，并增加 443 端口严格白名单限制
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
            if parsed.scheme != "https" or hostname != "api.discogs.com" or (port not in (None, 443)):
                raise DiscogsAPIError("非法的 Discogs 资源地址，拒绝请求。")
            return endpoint_or_url
            
        path = endpoint_or_url if endpoint_or_url.startswith("/") else f"/{endpoint_or_url}"
        return f"{self.base_url}{path}"
        
    def _normalize_web_url(self, uri: str, fallback: str = "暂无链接") -> str:
        if not uri:
            return fallback
        
        if uri.startswith("/"):
            return f"https://www.discogs.com{uri}"
        
        if uri.startswith(("http://", "https://")):
            parsed = urlparse(uri)
            # 🚀 优化：主机名显式转小写
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and hostname in self._allowed_web_domains:
                return uri
            else:
                logger.warning(f"Blocked suspicious Discogs web URI: {str(uri)[:50]}...")
                if fallback.startswith(("http://", "https://")):
                    return fallback
                return f"{fallback} (由于安全原因已拦截链接)"
        
        return fallback

    def _parse_query(self, raw_query: str) -> dict:
        valid_keys = {
            "type", "title", "release_title", "credit", "artist", "anv", 
            "label", "genre", "style", "country", "year", "format", 
            "catno", "barcode", "track"
        }
        params = {}
        
        try:
            words = shlex.split(raw_query)
        except ValueError:
            raise DiscogsException(
                '查询语法有误：引号可能未正确闭合。\n'
                '示例：artist:"Pink Floyd" year:1973'
            )
            
        q_words = []
        for word in words:
            if ":" in word:
                parts = word.split(":", 1)
                if len(parts) == 2 and parts[0] in valid_keys:
                    key = parts[0]
                    value = parts[1].strip()
                    if value:
                        params[key] = value
                        continue
            q_words.append(word)
            
        if q_words:
            params["q"] = " ".join(q_words)
            
        return params

    def _validate_input(self, query: str) -> Optional[str]:
        if not query or not query.strip():
            return "请输入查询关键词，例如：/音乐 nevermind year:1991"
        if len(query) > 200:
            return "查询内容过长，请精简到 200 字符以内。"
        return None

    async def _make_request(self, endpoint_or_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._normalize_discogs_url(endpoint_or_url)
        session = await self._get_session()
        request_headers = {**self.static_headers, **self._get_auth_header()}
        
        try:
            async with session.get(url, params=params, headers=request_headers) as response:
                
                limit = response.headers.get("X-Discogs-Ratelimit")
                used = response.headers.get("X-Discogs-Ratelimit-Used")
                remaining = response.headers.get("X-Discogs-Ratelimit-Remaining")
                
                if limit and remaining:
                    logger.debug(f"Discogs API Rate Limit: {used}/{limit} used. {remaining} remaining.")
                
                if response.status == 200:
                    try:
                        return await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        raise DiscogsAPIError("Discogs 返回了非 JSON 格式的数据。")
                
                chunk = await response.content.read(1024)
                text = chunk.decode('utf-8', errors='replace')
                clean_text = " ".join(text.split())[:100]
                
                if response.status == 400:
                    raise DiscogsAPIError(f"请求参数错误。详情: {clean_text}")
                elif response.status == 401:
                    raise DiscogsAuthError("身份验证失败，请检查 Token 是否正确。")
                elif response.status == 403:
                    raise DiscogsAPIError("请求被拒绝，可能是权限不足或被封禁。")
                elif response.status == 404:
                    raise DiscogsAPIError("未找到相关资源。")
                elif response.status == 429:
                    logger.warning(f"Discogs Rate Limit Hit: Limit={limit}, Used={used}, Remaining={remaining}")
                    retry_after = response.headers.get("Retry-After")
                    msg = "请求过于频繁，触发 Discogs 速率限制"
                    msg += f"，建议等待 {retry_after} 秒后再试。" if retry_after else "，请稍后重试。"
                    raise DiscogsRateLimitError(msg)
                elif 500 <= response.status < 600:
                    raise DiscogsAPIError("Discogs 服务暂时不可用。")
                else:
                    raise DiscogsAPIError(f"HTTP {response.status}: {clean_text}")

        except asyncio.TimeoutError:
            raise DiscogsException("请求 Discogs 超时，请检查网络。")
        except aiohttp.ClientConnectionError:
            raise DiscogsException("无法连接到 Discogs 服务器。")
        except aiohttp.ClientError as e:
            # 🚀 优化：完全收敛底层异常，日志自留，提示更清爽
            logger.warning(f"Discogs client error: {type(e).__name__} - {e}")
            raise DiscogsException("网络请求失败，请稍后重试。")

    def _format_best_match_message(self, data: Dict[str, Any], is_detailed: bool, note: str = "") -> str:
        raw_title = data.get("title", "未知标题")
        year = data.get("year", "未知年份")
        full_url = self._normalize_web_url(data.get('uri'))
        
        album_title = raw_title
        artist_info = "未知"
        
        if is_detailed:
            artists_list = data.get("artists", [])
            artist_names = [re.sub(r'\s\(\d+\)$', '', a.get("name", "未知")) for a in artists_list[:3]]
            artist_info = ", ".join(artist_names) if artist_names else "未知"
        else:
            if " - " in raw_title:
                parts = raw_title.split(" - ", 1)
                artist_info = parts[0].strip()
                album_title = parts[1].strip()
        
        header = "🥇 【最佳匹配】"
        if not is_detailed:
            header += "(基础信息)"
        if note:
            header += f" ({note})"
            
        msg_parts = [
            f"{header}",
            f"   《{album_title}》 ({year})",
            f"   👤 艺术家: {artist_info}"
        ]
        
        if is_detailed:
            genres = ", ".join(data.get("genres", [])) or "未知"
            msg_parts.append(f"   🎸 流派: {genres}")
            
        msg_parts.append(f"   🔗 {full_url}")
        return "\n".join(msg_parts)

    @filter.command("音乐")
    async def search_music(self, event: AstrMessageEvent, *, query: str = ""):
        if not self._is_token_configured():
            yield event.plain_result("请先在配置中填写 Discogs Token 才可使用搜索。")
            return

        error_msg = self._validate_input(query)
        if error_msg:
            yield event.plain_result(error_msg)
            return

        try:
            params = self._parse_query(query)
            params["per_page"] = 3  

            search_data = await self._make_request("/database/search", params=params)
            results = search_data.get("results", [])
            
            if not results:
                yield event.plain_result(f"未找到与 '{query}' 相关的音乐记录。")
                return

            reply_parts = [f"🎵 关于 '{query}' 的搜索结果：\n"]
            
            best_res = results[0]
            top_match_msg = ""
            
            if best_res.get("resource_url"):
                try:
                    detail_data = await self._make_request(best_res.get("resource_url"))
                    top_match_msg = self._format_best_match_message(detail_data, is_detailed=True)
                except DiscogsException as sub_e:
                    logger.warning(f"Discogs API error fetching detail for top match: {str(sub_e)[:50]}...")
                    note = str(sub_e) if isinstance(sub_e, (DiscogsAuthError, DiscogsRateLimitError)) else "详情获取失败"
                    top_match_msg = self._format_best_match_message(best_res, is_detailed=False, note=note)
                except Exception:
                    logger.exception("Unexpected error fetching detail for top match")
                    top_match_msg = self._format_best_match_message(best_res, is_detailed=False, note="详情获取失败")
            else:
                top_match_msg = self._format_best_match_message(best_res, is_detailed=False)
            
            reply_parts.append(top_match_msg)
            reply_parts.append("") 

            other_candidates = []
            for i, res in enumerate(results[1:3], 1):
                title = res.get("title", "未知标题/艺术家")
                year = res.get("year", "未知")
                full_url = self._normalize_web_url(res.get("uri"))
                other_candidates.append(f"   {i}. 《{title}》 ({year}) - {full_url}")

            if other_candidates:
                reply_parts.append("📚 【其他候选】")
                reply_parts.extend(other_candidates)

            yield event.plain_result("\n".join(reply_parts))

        except DiscogsException as e:
            yield event.plain_result(f"查询失败: {str(e)}")
        except Exception:
            logger.exception("Discogs music search encountered an unexpected error")
            yield event.plain_result("发生未知内部错误，请稍后重试。")

    @filter.command("黑胶价格")
    async def check_vinyl_price(self, event: AstrMessageEvent, *, query: str = ""):
        if not self._is_token_configured():
            yield event.plain_result("请先在配置中填写 Discogs Token 才可使用查价。")
            return

        error_msg = self._validate_input(query)
        if error_msg:
            yield event.plain_result(error_msg)
            return

        try:
            params = self._parse_query(query)
            params["format"] = "vinyl"
            params["type"] = "release"
            params["per_page"] = 3

            search_data = await self._make_request("/database/search", params=params)
            results = search_data.get("results", [])
            
            if not results:
                yield event.plain_result(f"未找到与 '{query}' 相关的黑胶唱片记录。")
                return
                
            reply_parts = [f"💿 关于 '{query}' 的黑胶市价 (前 {len(results)} 个匹配)：\n"]

            sem = asyncio.Semaphore(2)

            async def fetch_price_info(index: int, search_res: Dict[str, Any]) -> str:
                release_id = search_res.get("id")
                search_title = search_res.get("title", "未知标题")
                search_year = search_res.get("year", "未知年份")
                prefix = f"{index}. 《{search_title}》 ({search_year})"
                
                if not release_id:
                    return f"{prefix}\n   ❌ 缺少发行版 ID，无法查询价格"
                
                async with sem:
                    try:
                        release_data = await self._make_request(f"/releases/{release_id}", params={"curr_abbr": "USD"})
                        
                        lowest_price = release_data.get("lowest_price")
                        num_for_sale = release_data.get("num_for_sale", 0)
                        
                        if num_for_sale > 0 and lowest_price is not None:
                            try:
                                price_text = f"${float(lowest_price):.2f} (USD)"
                                market_info = f"💰 {num_for_sale} 张在售 | 最低起售价约: {price_text}"
                            except (TypeError, ValueError):
                                market_info = "💰 价格数据解析异常"
                        else:
                            market_info = "🪹 目前市场上暂无卖家出售"

                        fallback_url = f"https://www.discogs.com/release/{release_id}"
                        detail_url = self._normalize_web_url(release_data.get("uri"), fallback_url)
                        
                        return f"{prefix}\n   {market_info}\n   🔗 详情: {detail_url}"
                        
                    except DiscogsException as sub_e:
                        return f"{prefix}\n   ❌ 获取市场价格失败: {str(sub_e)}"
                    except Exception:
                        logger.exception(f"Unexpected error fetching price for release {release_id}")
                        return f"{prefix}\n   ❌ 查价发生内部错误"

            tasks = [fetch_price_info(i, res) for i, res in enumerate(results, 1)]
            
            price_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in price_results:
                if isinstance(res, Exception):
                    logger.error(f"A price fetching task failed abruptly: {repr(res)}")
                    reply_parts.append("   ❌ 某条价格查询任务异常中止，请稍后重试。")
                else:
                    reply_parts.append(res)

            yield event.plain_result("\n\n".join(reply_parts))

        except DiscogsException as e:
            yield event.plain_result(f"查价失败: {str(e)}")
        except Exception:
            logger.exception("Discogs vinyl price check encountered an unexpected error")
            yield event.plain_result("发生未知内部错误，请稍后重试。")

    async def terminate(self):
        # 🚀 优化：缩小锁的范围，仅保护引用交接，避免 I/O 阻塞事件循环
        async with self._session_lock:
            session = self.session
            self.session = None

        if session and not session.closed:
            await session.close()
