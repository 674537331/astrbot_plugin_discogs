import aiohttp
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
        
        # Discogs 强制要求包含 User-Agent
        self.headers = {
            "User-Agent": "AstrBotDiscogsPlugin/1.1 +https://github.com/AstrBotDevs"
        }
        if self.token:
            self.headers["Authorization"] = f"Discogs token={self.token}"

    def _parse_query(self, raw_query: str) -> dict:
        """
        高级搜索解析器。
        将用户输入的 "nevermind year:1991 format:album" 解析为 API 接受的 params 字典。
        支持的维度：type, title, artist, label, genre, style, country, year, format, barcode 等。
        """
        valid_keys = [
            "type", "title", "release_title", "credit", "artist", "anv", 
            "label", "genre", "style", "country", "year", "format", 
            "catno", "barcode", "track"
        ]
        params = {}
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

    async def _make_request(self, endpoint: str, params: dict = None) -> dict:
        """统一的异步 HTTP 请求处理器"""
        if not self.token and "/database/search" in endpoint:
            raise Exception("Discogs 搜索功能必须配置 Token。请前往 WebUI 插件设置页面填写。")

        url = f"{self.base_url}{endpoint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
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
            params["per_page"] = 1  # 我们只需要最精准的第一条记录

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

            # 2. 获取具体资源信息 (Release 或 Master)
            endpoint = resource_url.replace(self.base_url, "")
            detail_data = await self._make_request(endpoint)
            
            title = detail_data.get("title", "未知标题")
            year = detail_data.get("year", "未知年份")
            genres = ", ".join(detail_data.get("genres", []))
            
            # 提取艺术家信息并截取简介防止刷屏
            artists_list = detail_data.get("artists", [])
            artist_info = "未知艺术家"
            if artists_list:
                first_artist = artists_list[0]
                artist_name = first_artist.get("name", "未知")
                # 可选：如果想查艺术家详情，可以在此处再发起一次对 first_artist["resource_url"] 的请求
                artist_info = f"{artist_name}"

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
