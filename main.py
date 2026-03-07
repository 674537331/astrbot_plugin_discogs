import asyncio
import aiohttp
import shlex
import re
from urllib.parse import urlparse
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# --- 自定义异常类 ---
class DiscogsException(Exception):
    """Discogs 插件基础异常"""
    pass

class DiscogsAuthError(DiscogsException):
    pass

class DiscogsRateLimitError(DiscogsException):
    pass

class DiscogsAPIError(DiscogsException):
    pass


@register("discogs_plugin", "YourName", "Discogs 音乐与黑胶查询", "1.4.1")
class DiscogsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = "https://api.discogs.com"
        self.session = None 
        self._session_lock = asyncio.Lock()
        
        self.headers = {
            "User-Agent": "AstrBotDiscogsPlugin/1.4.1 +https://github.com/AstrBotDevs"
        }
        self._update_auth_header()

    def _update_auth_header(self):
        token = self.config.get("discogs_token", "")
        if token:
            self.headers["Authorization"] = f"Discogs token={token}"
        else:
            self.headers.pop("Authorization", None)
            
    def _is_token_configured(self) -> bool:
        """语义化：直接检查配置中是否包含有效的 token"""
        return bool(self.config.get("discogs_token", "").strip())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            async with self._session_lock:
                if self.session is None or self.session.closed:
                    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
                    self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    def _normalize_discogs_url(self, endpoint_or_url: str) -> str:
        if endpoint_or_url.startswith(("http://", "https://")):
            parsed = urlparse(endpoint_or_url)
            if parsed.scheme != "https" or parsed.netloc != "api.discogs.com":
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
            return uri
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
            raise DiscogsException('查询语法有误：引号可能未正确闭合。示例：artist:"Pink Floyd" year:1973')
            
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

    async def _make_request(self, endpoint_or_url: str, params: dict = None) -> dict:
        self._update_auth_header()
        
        url = self._normalize_discogs_url(endpoint_or_url)
        session = await self._get_session()
        request_headers = dict(self.headers)
        
        try:
            async with session.get(url, params=params, headers=request_headers) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        raise DiscogsAPIError("Discogs 返回了非 JSON 格式的数据。")
                
                text = await response.text()
                clean_text = " ".join(text.split())[:100]
                
                if response.status == 400:
                    raise DiscogsAPIError(f"请求参数错误，请检查搜索维度。详情: {clean_text}")
                elif response.status == 401:
                    raise DiscogsAuthError("身份验证失败，请检查 WebUI 中的 Discogs Token 是否正确。")
                elif response.status == 403:
                    raise DiscogsAPIError("请求被 Discogs 拒绝，可能是权限不足。")
                elif response.status == 404:
                    raise DiscogsAPIError("未找到相关资源。")
                elif response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    msg = "请求过于频繁，触发 Discogs 速率限制"
                    msg += f"，建议等待 {retry_after} 秒后再试。" if retry_after else "，请稍后重试。"
                    raise DiscogsRateLimitError(msg)
                elif 500 <= response.status < 600:
                    raise DiscogsAPIError("Discogs 服务暂时不可用，请稍后重试。")
                else:
                    raise DiscogsAPIError(f"HTTP {response.status}: {clean_text}")

        except asyncio.TimeoutError:
            raise DiscogsException("请求 Discogs 超时，请检查网络或稍后重试。")
        except aiohttp.ClientConnectionError:
            raise DiscogsException("无法连接到 Discogs 服务器，请检查网络连通性。")
        except aiohttp.ClientError as e:
            raise DiscogsException(f"网络请求引发内部异常: {str(e)}")

    def _validate_input(self, query: str) -> Optional[str]:
        if not query or not query.strip():
            return "请输入查询关键词，例如：/音乐 nevermind year:1991"
        if len(query) > 200:
            return "查询内容过长，请精简到 200 字符以内后重试。"
        return None

    @filter.command("音乐")
    async def search_music(self, event: AstrMessageEvent, *, query: str = ""):
        self._update_auth_header()
        if not self._is_token_configured():
            yield event.plain_result("请先在 WebUI 插件配置中填写 Discogs Token 才可使用搜索功能。")
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
            other_candidates = []
            
            for i, res in enumerate(results[:3], 1):
                if i == 1:
                    # 尝试拉取最佳匹配的详细信息
                    detail_fetched = False
                    if res.get("resource_url"):
                        try:
                            detail_data = await self._make_request(res.get("resource_url"))
                            title = detail_data.get("title", "未知标题")
                            year = detail_data.get("year", "未知年份")
                            genres = ", ".join(detail_data.get("genres", []))
                            
                            artists_list = detail_data.get("artists", [])
                            artist_names = [re.sub(r'\s\(\d+\)$', '', a.get("name", "未知")) for a in artists_list[:3]]
                            artist_info = ", ".join(artist_names) if artist_names else "未知艺术家"
                            
                            full_url = self._normalize_web_url(detail_data.get('uri'))
                            
                            reply_parts.append(
                                f"🥇 【最佳匹配】\n"
                                f"   《{title}》 ({year})\n"
                                f"   👤 艺术家: {artist_info}\n"
                                f"   🎸 流派: {genres}\n"
                                f"   🔗 {full_url}\n"
                            )
                            detail_fetched = True
                        except DiscogsException as sub_e:
                            logger.warning(f"Discogs API error fetching detail for top match: {sub_e}")
                        except Exception:
                            logger.exception("Unexpected error fetching detail for top match")
                    
                    # 如果缺少 resource_url 或拉取失败，触发优雅降级
                    if not detail_fetched:
                        title = res.get("title", "未知标题/艺术家")
                        year = res.get("year", "未知")
                        full_url = self._normalize_web_url(res.get("uri"))
                        reply_parts.append(
                            f"🥇 【最佳匹配】(基础信息)\n"
                            f"   《{title}》 ({year})\n"
                            f"   🔗 {full_url}\n"
                        )
                    continue

                # 备选结果
                title = res.get("title", "未知标题/艺术家")
                year = res.get("year", "未知")
                full_url = self._normalize_web_url(res.get("uri"))

                other_candidates.append(f"   {len(other_candidates) + 1}. 《{title}》 ({year}) - {full_url}")

            if other_candidates:
                reply_parts.append("📚 【其他候选】\n" + "\n".join(other_candidates))

            yield event.plain_result("\n".join(reply_parts))

        except DiscogsException as e:
            yield event.plain_result(f"查询失败: {str(e)}")
        except Exception:
            logger.exception("Discogs music search encountered an unexpected error")
            yield event.plain_result("发生未知内部错误，请稍后重试。")

    @filter.command("黑胶价格")
    async def check_vinyl_price(self, event: AstrMessageEvent, *, query: str = ""):
        self._update_auth_header()
        if not self._is_token_configured():
            yield event.plain_result("请先在 WebUI 插件配置中填写 Discogs Token 才可使用查价功能。")
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
                
            reply_parts = [f"💿 关于 '{query}' 的黑胶市价 (前 {len(results[:3])} 个匹配)：\n"]

            for i, res in enumerate(results[:3], 1):
                release_id = res.get("id")
                search_title = res.get("title", "未知标题")
                search_year = res.get("year", "未知年份")
                
                if not release_id:
                    reply_parts.append(f"{i}. 《{search_title}》\n   ❌ 缺少发行版 ID，无法查询价格")
                    continue
                
                try:
                    release_data = await self._make_request(f"/releases/{release_id}", params={"curr_abbr": "USD"})
                    
                    lowest_price = release_data.get("lowest_price")
                    num_for_sale = release_data.get("num_for_sale", 0)
                    
                    if num_for_sale > 0 and lowest_price is not None:
                        try:
                            price_text = f"${float(lowest_price):.2f} (USD)"
                        except (TypeError, ValueError):
                            price_text = "价格数据解析异常"
                            
                        market_info = f"💰 {num_for_sale} 张在售 | 最低起售价约: {price_text}"
                    else:
                        market_info = "🪹 目前市场上暂无卖家出售"

                    detail_url = self._normalize_web_url(release_data.get("uri"), f"https://www.discogs.com/release/{release_id}")
                    reply_parts.append(f"{i}. 《{search_title}》 ({search_year})\n   {market_info}\n   🔗 详情: {detail_url}")
                    
                except DiscogsException as sub_e:
                    reply_parts.append(f"{i}. 《{search_title}》\n   ❌ 获取市场价格失败: {str(sub_e)}")
                except Exception:
                    logger.exception("Unexpected error fetching price for release %s", release_id)
                    reply_parts.append(f"{i}. 《{search_title}》\n   ❌ 查价发生内部错误")

            yield event.plain_result("\n\n".join(reply_parts))

        except DiscogsException as e:
            yield event.plain_result(f"查价失败: {str(e)}")
        except Exception:
            logger.exception("Discogs vinyl price check encountered an unexpected error")
            yield event.plain_result("发生未知内部错误，请稍后重试。")

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
