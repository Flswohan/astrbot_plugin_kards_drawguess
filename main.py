import os
import json
import asyncio
import random
import shutil
from io import BytesIO
from datetime import datetime, timedelta
from collections import defaultdict
import aiohttp
from PIL import Image
from skimage.metrics import structural_similarity as ssim
import numpy as np
import imagehash

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp


class KardsDrawGuess(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_dir = os.path.dirname(__file__)
        self.data_dir = os.path.join(self.base_dir, "data")
        self.cards_json_path = os.path.join(self.data_dir, "cards.json")
        self.images_dir = os.path.join(self.data_dir, "cards_images")
        self.temp_dir = os.path.join(self.base_dir, "temp")
        os.makedirs(self.temp_dir, exist_ok=True)

        # 加载卡牌信息
        self.cards_db = self.load_cards_db()
        # 获取所有有图片的卡牌代码
        self.available_cards = self.get_available_cards()
        logger.info(f"KARDS画卡牌插件加载，可用卡牌数：{len(self.available_cards)}")

        # 游戏状态管理：{group_id: game_session}
        self.games = {}

    def load_cards_db(self):
        if os.path.exists(self.cards_json_path):
            with open(self.cards_json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def get_available_cards(self):
        """返回有图片且存在于数据库的卡牌代码列表"""
        valid = []
        if not os.path.exists(self.images_dir):
            return valid
        for fname in os.listdir(self.images_dir):
            if fname.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                code = os.path.splitext(fname)[0]
                if code in self.cards_db:
                    valid.append(code)
        return valid

    # ==================== 游戏会话类 ====================
    class GameSession:
        def __init__(self, initiator_id, initiator_name, card_code, card_name, players=None):
            self.initiator_id = initiator_id
            self.initiator_name = initiator_name
            self.card_code = card_code          # 卡牌代码，用于加载原图
            self.card_name = card_name          # 卡牌名称，作为题目
            self.players = players or []        # 玩家列表 [{"id":"xxx", "name":"xxx", "image_path":None}]
            self.current_index = 0              # 当前轮到第几个玩家（索引）
            self.started = False
            self.finished = False
            self.turn_start_time = None         # 当前回合开始时间
            self.timer_task = None              # 计时器任务
            self.threshold = 0.5                # 相似度阈值，可调

        def next_player(self):
            self.current_index += 1
            if self.current_index >= len(self.players):
                return None
            return self.players[self.current_index]

        def is_last_player(self):
            return self.current_index == len(self.players) - 1

    # ==================== 图像相似度计算 ====================
    def compare_images(self, img1_path, img2_path):
        """
        计算两张图片的SSIM（结构相似性），返回0-1之间的浮点数
        """
        try:
            # 加载并转为灰度，调整到相同大小
            img1 = Image.open(img1_path).convert('L')
            img2 = Image.open(img2_path).convert('L')
            # 统一尺寸（为了SSIM）
            size = (256, 256)
            img1 = img1.resize(size, Image.Resampling.LANCZOS)
            img2 = img2.resize(size, Image.Resampling.LANCZOS)
            arr1 = np.array(img1)
            arr2 = np.array(img2)
            # 计算SSIM
            score = ssim(arr1, arr2, data_range=255)
            return score
        except Exception as e:
            logger.error(f"SSIM计算失败: {e}")
            return 0.0

    # ==================== 游戏管理 ====================
    def create_game(self, group_id, initiator_id, initiator_name):
        """创建游戏，随机选一张卡牌作为题目"""
        if not self.available_cards:
            return None, "卡牌图片库为空，无法开始游戏"

        card_code = random.choice(self.available_cards)
        card_info = self.cards_db.get(card_code, {})
        card_name = card_info.get("name", card_code)

        session = self.GameSession(initiator_id, initiator_name, card_code, card_name)
        session.players.append({"id": initiator_id, "name": initiator_name, "image_path": None})
        self.games[group_id] = session
        return session, f"已创建游戏，题目已定！当前玩家：{initiator_name}（房主）\n请其他玩家发送 `/加入画卡牌` 加入。"

    def join_game(self, group_id, player_id, player_name):
        session = self.games.get(group_id)
        if not session:
            return None, "当前群没有进行中的游戏，请先发送 `/画卡牌` 创建。"
        if session.started:
            return None, "游戏已经开始，无法加入。"
        if any(p["id"] == player_id for p in session.players):
            return None, "你已经加入了。"
        session.players.append({"id": player_id, "name": player_name, "image_path": None})
        return session, f"{player_name} 加入成功！当前共 {len(session.players)} 人。"

    def start_game(self, group_id, initiator_id):
        session = self.games.get(group_id)
        if not session:
            return None, "没有游戏。"
        if session.started:
            return None, "游戏已开始。"
        if session.initiator_id != initiator_id:
            return None, "只有房主可以开始游戏。"
        if len(session.players) < 2:
            return None, "至少需要2名玩家。"
        # 随机打乱玩家顺序（房主保留在第一个？或全部打乱）
        # 为了公平，全部打乱，但房主也可以参与
        random.shuffle(session.players)
        # 但确保房主不是第一个？或者无所谓，我们就按随机顺序
        session.started = True
        session.current_index = 0
        # 开始第一个玩家的回合
        self._start_turn(group_id)
        return session, f"游戏开始！绘画顺序：\n" + "\n".join([f"{i+1}. {p['name']}" for i, p in enumerate(session.players)])

    def _start_turn(self, group_id):
        session = self.games.get(group_id)
        if not session or session.finished:
            return
        player = session.players[session.current_index]
        # 设置计时器，1分钟
        session.turn_start_time = datetime.now()
        # 发送提示消息（这里我们通过外部发送，在命令处理中发送，因为这里无法直接yield）
        # 我们返回需要发送的消息，由调用者处理
        return player

    def submit_drawing(self, group_id, player_id, image_url):
        """玩家提交画作，保存图片并判断是否最后一人"""
        session = self.games.get(group_id)
        if not session or not session.started or session.finished:
            return None, "游戏未开始或已结束。"
        current_player = session.players[session.current_index]
        if current_player["id"] != player_id:
            return None, "还没轮到你画哦。"

        # 下载图片
        try:
            # 使用aiohttp下载
            async def download():
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(image_url) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()
                            return img_data
                        return None
            # 由于这里是同步函数，需要运行异步，我们使用asyncio.run，但最好改为异步方法
            # 实际我们会在命令处理中异步调用，所以把下载逻辑放在外面，这里只处理文件保存
            # 我们改变设计：提交命令中直接传入图片数据，而不是URL
            # 下面调整
        except:
            pass
        # 我们改为在命令处理中下载并传递文件路径

        return session, "提交成功"

    # 我们重新设计：将提交处理放在命令中，因为涉及异步下载

    # ==================== 命令处理 ====================
    @filter.command("画卡牌")
    async def cmd_create(self, event: AstrMessageEvent):
        '''创建新游戏
        用法：/画卡牌
        然后其他玩家用 /加入画卡牌 加入，房主用 /开始画卡牌 开始'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该命令仅支持群聊。")
            return
        if group_id in self.games and not self.games[group_id].finished:
            yield event.plain_result("当前群已有游戏进行中，请等待结束。")
            return
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        session, msg = self.create_game(group_id, sender_id, sender_name)
        if session:
            yield event.plain_result(msg)
            # 同时发送题目提示（不显示卡牌图片，只显示名称和属性）
            card_info = self.cards_db.get(session.card_code, {})
            extra_info = f"国家：{card_info.get('nation','未知')}，费用：{card_info.get('cost','?')}，类型：{card_info.get('type','未知')}"
            yield event.plain_result(f"🎨 本次要画的卡牌是：**{session.card_name}**\n提示：{extra_info}\n请在不看原图的情况下凭记忆或想象绘画！")
        else:
            yield event.plain_result(f"❌ {msg}")

    @filter.command("加入画卡牌")
    async def cmd_join(self, event: AstrMessageEvent):
        '''加入游戏
        用法：/加入画卡牌'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该命令仅支持群聊。")
            return
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        session, msg = self.join_game(group_id, sender_id, sender_name)
        if session:
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ {msg}")

    @filter.command("开始画卡牌")
    async def cmd_start(self, event: AstrMessageEvent):
        '''房主开始游戏
        用法：/开始画卡牌'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该命令仅支持群聊。")
            return
        sender_id = event.get_sender_id()
        session, msg = self.start_game(group_id, sender_id)
        if session:
            yield event.plain_result(msg)
            # 开始第一个回合
            first_player = session.players[0]
            yield event.plain_result(f"🖌️ 请 **{first_player['name']}** 开始作画！\n你有一分钟时间，画完后发送 **/上传画作** 并附上图片。")
            # 启动计时器，1分钟后超时
            asyncio.create_task(self._turn_timeout(group_id, first_player['id']))
        else:
            yield event.plain_result(f"❌ {msg}")

    async def _turn_timeout(self, group_id, player_id):
        """超时处理，1分钟未提交则跳过"""
        await asyncio.sleep(60)
        session = self.games.get(group_id)
        if not session or session.finished:
            return
        # 检查当前玩家是否还是该玩家
        if session.players[session.current_index]['id'] == player_id:
            # 超时，自动跳过
            await self._next_turn(group_id, timeout=True)

    async def _next_turn(self, group_id, timeout=False):
        """切换到下一个玩家，或结束游戏"""
        session = self.games.get(group_id)
        if not session or session.finished:
            return
        # 如果超时，当前玩家未提交，我们记录为未画
        if timeout:
            # 通知群
            await self._send_message(group_id, f"⏰ {session.players[session.current_index]['name']} 超时未画，自动跳过。")
            # 移动到下一个
            session.current_index += 1
            if session.current_index >= len(session.players):
                # 所有人都画完了，进行审核
                await self._final_review(group_id)
                return
            # 通知下一位
            next_player = session.players[session.current_index]
            await self._send_message(group_id, f"🖌️ 轮到 **{next_player['name']}** 作画！一分钟倒计时开始。")
            asyncio.create_task(self._turn_timeout(group_id, next_player['id']))
        else:
            # 正常提交后的切换
            session.current_index += 1
            if session.current_index >= len(session.players):
                await self._final_review(group_id)
                return
            next_player = session.players[session.current_index]
            await self._send_message(group_id, f"🖌️ 轮到 **{next_player['name']}** 作画！一分钟倒计时开始。")
            asyncio.create_task(self._turn_timeout(group_id, next_player['id']))

    async def _send_message(self, group_id, msg):
        """发送群消息（由于无法在非命令上下文中直接yield，我们通过context发送）"""
        # 这里使用context的send_message方法（需要获取context实例）
        # 我们保存context引用
        await self.context.send_message(group_id, msg)

    @filter.command("上传画作")
    async def cmd_upload(self, event: AstrMessageEvent):
        '''上传你的画作
        用法：/上传画作 并附上图片'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该命令仅支持群聊。")
            return
        session = self.games.get(group_id)
        if not session or not session.started or session.finished:
            yield event.plain_result("当前没有进行中的游戏。")
            return
        sender_id = event.get_sender_id()
        current_player = session.players[session.current_index]
        if current_player['id'] != sender_id:
            yield event.plain_result("还没轮到你画。")
            return

        # 检查是否有图片
        if not event.message_obj or not hasattr(event.message_obj, 'message'):
            yield event.plain_result("请附上一张图片！")
            return
        image_segments = [seg for seg in event.message_obj.message if isinstance(seg, Comp.Image)]
        if not image_segments:
            yield event.plain_result("请附上一张图片！")
            return

        img_url = image_segments[0].url
        if not img_url:
            yield event.plain_result("无法获取图片URL。")
            return

        # 下载图片并保存到临时目录
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(img_url) as resp:
                    if resp.status != 200:
                        yield event.plain_result("图片下载失败。")
                        return
                    img_data = await resp.read()
                    # 保存为临时文件
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
                    temp_path = os.path.join(self.temp_dir, f"{sender_id}_{timestamp}.png")
                    with open(temp_path, 'wb') as f:
                        f.write(img_data)
                    # 记录到玩家信息
                    current_player['image_path'] = temp_path
                    yield event.plain_result(f"✅ {current_player['name']} 的画作已接收！")
                    # 检查是否最后一人
                    if session.is_last_player():
                        # 直接进入审核
                        await self._final_review(group_id)
                    else:
                        # 切换到下一位
                        await self._next_turn(group_id, timeout=False)
        except Exception as e:
            logger.error(f"上传画作失败: {e}")
            yield event.plain_result(f"上传失败: {str(e)}")

    async def _final_review(self, group_id):
        """最后一人画完，进行审核"""
        session = self.games.get(group_id)
        if not session:
            return
        # 获取最后一位玩家的画作路径
        last_player = session.players[-1]
        drawing_path = last_player.get('image_path')
        if not drawing_path or not os.path.exists(drawing_path):
            await self._send_message(group_id, "最后一位玩家未提交画作，游戏结束。")
            self.games.pop(group_id, None)
            return

        # 加载原卡牌图片
        original_path = os.path.join(self.images_dir, f"{session.card_code}.png")
        if not os.path.exists(original_path):
            # 尝试其他扩展名
            for ext in ['.jpg', '.jpeg', '.webp']:
                alt_path = os.path.join(self.images_dir, f"{session.card_code}{ext}")
                if os.path.exists(alt_path):
                    original_path = alt_path
                    break
            else:
                await self._send_message(group_id, "❌ 找不到目标卡牌的原图，无法审核。")
                self.games.pop(group_id, None)
                return

        # 计算相似度
        similarity = self.compare_images(drawing_path, original_path)
        threshold = session.threshold
        is_win = similarity >= threshold

        # 清理临时文件
        try:
            os.remove(drawing_path)
        except:
            pass

        # 发送结果
        result_msg = f"🎨 绘画审核结果：\n相似度：{similarity*100:.1f}%\n"
        if is_win:
            result_msg += f"🎉 恭喜！相似度达到 {threshold*100}%，游戏胜利！"
        else:
            result_msg += f"😢 相似度未达到 {threshold*100}%，游戏失败。\n以下是原卡牌："
        await self._send_message(group_id, result_msg)

        # 如果失败，可以发送原图
        if not is_win:
            # 发送原图（需要上传图片，我们可借助context发送文件）
            # 由于AstrBot可能不支持直接发送文件，我们可以使用图片链接或base64，简单起见，仅文字提示
            await self._send_message(group_id, f"原卡牌名称：{session.card_name}，代码：{session.card_code}")

        # 清理游戏
        self.games.pop(group_id, None)

    async def terminate(self):
        logger.info("KARDS画卡牌插件已卸载")
