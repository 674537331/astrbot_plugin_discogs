import aiohttp
import re
import shlex
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

@register("discogs_plugin", "YourName", "Discogs 音乐与黑胶查询", "1.1.0")
class DiscogsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.token = self.config.get("discogs_token", "")
        self.base_url = "https://api.discogs.com"
        
        # 预先声明 session，用于后续复用提升性能
        self.session = None 
        
        # Discogs 强制要求包含 User-Agent
        self.headers = {
            "User-Agent": "AstrBotDiscogsPlugin/1.1 +https://github.com/AstrBotDevs"
        }
        if self.token:
            self.headers["Authorization"] = f"Discogs token={self.token}"

    async def _get_session(self) -> aiohttp.ClientSession:
        """懒加载获取全局可复用的 aiohttp Session"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers)
        return self.session

    def _parse_query(self, raw_query: str) -> dict:
        """
        高级搜索解析器。
        使用 shlex 库支持带空格和引号的值，例如: artist:"Pink Floyd" year:1973
        """
        valid_keys = [
            "type", "title", "release_title", "credit", "artist", "anv", 
            "label", "genre", "style", "country", "year", "format", 
            "catno", "barcode", "track"
        ]
        params = {}
        
        try:
            # shlex.split 会自动识别引号内的空格作为整体
            words = shlex.split(raw_query)
        except ValueError:
            # 兜底：如果用户引号没闭合导致报错，退回普通按空格分割
            words = raw_query.split()
            
        q_words = []
        
        for word in words:
            if ":" in word:
                parts = word.split(":", 1)
                if len(parts) == 2 and parts[0] in valid_keys:
                    params[parts[0]] = parts[1]
                    continue
            q_words.append(word)
            
        if q_words:
            params["q"] = " ".join(q_words)
            
        return params

    async def _make_request(self, endpoint_or_url: str, params: dict = None) -> dict:
        """统一的异步 HTTP 请求处理器，支持相对路径和绝对 URL"""
        if not self.token and "/database/search" in endpoint_or_url:
            raise Exception("Discogs 搜索功能必须配置 Token。请前往 WebUI 插件设置页面填写。")

        # 智能判断是相对路径还是绝对路径
        if endpoint_or_url.startswith("http"):
            url = endpoint_or_url
        else:
            url = f"{self.base_url}{endpoint_or_url}"
            
        session = await self._get_session()
        
        async with session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 401:
                raise Exception("身份验证失败，请检查 Discogs Token 是否正确。")
            elif response.status == 404:
                raise Exception("未找到相关资源。")
            elif response.status == 429:
                raise Exception("请求太频繁，触发了 Discogs 速率限制，请稍后重试。")
            else:
                raise Exception(f"请求失败，HTTP 状态码: {response.status}")

    @filter.command("音乐")
    async def search_music(self, event: AstrMessageEvent, *, query: str):
        """音乐核心库查询。支持维度过滤(如: year:1991)。用法: /音乐 <关键词>"""
        try:
            params = self._parse_query(query)
            params["per_page"] = 1  # 只需要最精准的第一条记录

            # 1. 执行数据库搜索
            search_data = await self._make_request("/database/search", params=params)
            results = search_data.get("results", [])
            
            if not results:
                yield event.plain_result(f"未找到与 '{query}' 相关的音乐记录。")
                return

            best_match = results[0]
            resource_url = best_match.get("resource_url")
            
            if not resource_url:
                 yield event.plain_result("找到了结果，但缺少详细信息链接。")
                 return

            # 2. 获取具体资源信息 (Release 或 Master)，直接传入完整 URL
            detail_data = await self._make_request(resource_url)
            
            title = detail_data.get("title", "未知标题")
            year = detail_data.get("year", "未知年份")
            genres = ", ".join(detail_data.get("genres", []))
            
            # 提取艺术家信息并截取简介防止刷屏
            artists_list = detail_data.get("artists", [])
            artist_info = "未知艺术家"
            if artists_list:
                first_artist = artists_list[0]
                raw_artist_name = first_artist.get("name", "未知")
                # 清洗 Discogs 特有的同名艺术家编号，例如 "John Doe (2)" -> "John Doe"
                clean_artist_name = re.sub(r'\s\(\d+\)$', '', raw_artist_name)
                artist_info = clean_artist_name

            reply = (
                f"🎵 检索结果：\n"
                f"标题: 《{title}》 ({year})\n"
                f"艺术家: {artist_info}\n"
                f"流派: {genres}\n"
                f"🔗 链接: {detail_data.get('uri', '暂无')}"
            )
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"Music search error: {e}")
            yield event.plain_result(f"查询失败: {str(e)}")


    @filter.command("黑胶价格")
    async def check_vinyl_price(self, event: AstrMessageEvent, *, query: str):
        """查询黑胶唱片市价。支持维度过滤(如: genre:rock)。用法: /黑胶价格 <关键词>"""
        try:
            params = self._parse_query(query)
            # 强制限定搜索格式为黑胶，并且只找 Release (Master 级别没有具体价格)
            params["format"] = "vinyl"
            params["type"] = "release"
            params["per_page"] = 1

            search_data = await self._make_request("/database/search", params=params)
            results = search_data.get("results", [])
            
            if not results:
                yield event.plain_result(f"未找到与 '{query}' 相关的黑胶唱片记录。")
                return
                
            best_match = results[0]
            release_id = best_match.get("id")
            title = best_match.get("title", "未知标题")
            
            # 通过 Release 接口获取市场报价数据，指定获取美元报价
            release_data = await self._make_request(f"/releases/{release_id}", params={"curr_abbr": "USD"})
            
            lowest_price = release_data.get("lowest_price")
            num_for_sale = release_data.get("num_for_sale", 0)
            year = release_data.get("year", "未知年份")
            
            if num_for_sale > 0 and lowest_price is not None:
                market_info = f"💰 当前有 {num_for_sale} 张在售，最低起售价约: ${lowest_price:.2f} (USD)"
            else:
                market_info = "🪹 目前 Discogs 市场上暂无卖家出售此黑胶。"

            reply = (
                f"💿 匹配黑胶：《{title}》({year})\n"
                f"{market_info}\n"
                f"🔗 详情与购买: https://www.discogs.com/release/{release_id}"
            )
            
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"Vinyl price error: {e}")
            yield event.plain_result(f"查价失败: {str(e)}")

    def terminate(self):
        """如果框架支持在插件卸载时调用，确保关闭 session 防内存泄漏"""
        import asyncio
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())
