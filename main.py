import os
import json
import random
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

class KardsPictionary(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 数据路径
        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        self.cards_db_path = os.path.join(self.data_dir, "cards.json")
        self.cards_db = self.load_cards_db()
        
        # 存储游戏状态 { group_id: {"painter": user_id, "answer": str, "nation": str, "cost": int, "timer": task} }
        self.games = {}
        
        logger.info(f"KARDS你画我猜插件加载完成，共加载 {len(self.cards_db)} 张卡牌")

    def load_cards_db(self):
        """加载卡牌数据库，只提取有名字的卡牌"""
        if not os.path.exists(self.cards_db_path):
            logger.warning(f"卡牌数据库不存在: {self.cards_db_path}")
            return {}
        with open(self.cards_db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 过滤掉没有 name 字段的无效卡牌
        valid_cards = {k: v for k, v in data.items() if v.get("name")}
        return valid_cards

    def get_random_card(self):
        """随机获取一张卡牌，返回 (卡牌代码, 卡牌数据)"""
        if not self.cards_db:
            return None, None
        code = random.choice(list(self.cards_db.keys()))
        return code, self.cards_db[code]

    @filter.command("画猜开始")
    async def start_game(self, event: AstrMessageEvent):
        '''开始一轮你画我猜
        用法：/画猜开始 [@画家]（如果不@，则默认发起者为画家）'''
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令。")
            return

        # 检查是否已有游戏
        if group_id in self.games:
            yield event.plain_result("⏳ 本群已有进行中的画猜游戏，请先使用 /画猜结束 结束当前游戏。")
            return

        # 确定画家
        painter = event.get_sender_id()
        painter_name = event.get_sender_name()
        
        # 检查是否 @ 了别人（简单解析，如果消息里有 @ 则取第一个被@的人）
        # 注：AstrBot 的 @ 是特殊格式，为了简化，我们默认发起者为画家
        # 如果用户输入 /画猜开始 @某人，我们可以尝试解析，但为了代码通用，先默认发起者。
        # 用户可以手动指定：/画猜开始 张三，我们则查找群成员？难度较大，先默认发起者。
        
        # 随机选卡
        code, card = self.get_random_card()
        if not card:
            yield event.plain_result("❌ 卡牌数据库为空，请先准备 cards.json 数据。")
            return

        answer = card.get("name")
        nation = card.get("nation", "未知国家")
        cost = card.get("cost", "?")
        card_type = card.get("type", "卡牌")

        # 存储游戏状态
        self.games[group_id] = {
            "painter": painter,
            "painter_name": painter_name,
            "answer": answer,
            "code": code,
            "nation": nation,
            "cost": cost,
            "card_type": card_type
        }

        # 1. 尝试私聊画家（发送答案）
        private_msg = f"🎨 你画我猜 - 你的关键词是：\n【{answer}】\n\n请在群里画出这张 {nation} {cost}费{card_type}，让大家猜吧！"
        try:
            # 尝试发送私聊
            yield self.context.send_message_to_user(painter, private_msg)
            private_status = "✅ 答案已通过私聊发送给你，请勿泄露！"
        except Exception as e:
            logger.warning(f"私聊画家失败: {e}")
            # 如果私聊失败，用一个简单的“倒置”或“提示词”在群里保护
            # 这里我们直接告诉画家在群里回复特定指令查看答案（或者干脆公开，但建议安全起见）
            # 既然私聊失败，我们只能在群里提示画家私聊机器人，或者换一种玩法：直接给提示
            private_status = f"⚠️ 无法私聊你，请主动私聊机器人输入【我的关键词】来获取要画的卡牌名。"

        # 2. 群聊广播（不包含答案）
        broadcast = (
            f"🎨 **你画我猜开始！**\n"
            f"👨‍🎨 画家：{painter_name}\n"
            f"📝 提示：{nation} | {cost}费 | {card_type}\n"
            f"🔍 大家根据画作猜卡牌名称！直接发送卡牌名即可。\n"
            f"{private_status}"
        )
        yield event.plain_result(broadcast)

        # 3. 设置超时（5分钟后自动结束）
        async def timeout_task():
            await asyncio.sleep(300)  # 300秒 = 5分钟
            if group_id in self.games:
                game = self.games[group_id]
                del self.games[group_id]
                # 无法在异步任务里直接 yield，用 context 发消息
                await self.context.send_message(
                    event.get_session_id(),
                    f"⏰ 时间到！本轮答案是：【{game['answer']}】\n下次再玩吧！"
                )
        asyncio.create_task(timeout_task())

    @filter.command("画猜结束")
    async def end_game(self, event: AstrMessageEvent):
        '''强制结束当前画猜游戏
        用法：/画猜结束'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令。")
            return

        if group_id not in self.games:
            yield event.plain_result("❌ 本群当前没有进行中的画猜游戏。")
            return

        game = self.games.pop(group_id)
        yield event.plain_result(f"🛑 游戏已结束。\n答案是：【{game['answer']}】")

    @filter.command("画猜提示")
    async def give_hint(self, event: AstrMessageEvent):
        '''获取额外提示（限画家使用）
        用法：/画猜提示'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令。")
            return

        if group_id not in self.games:
            yield event.plain_result("❌ 本群当前没有进行中的画猜游戏。")
            return

        game = self.games[group_id]
        sender_id = event.get_sender_id()
        
        if sender_id != game["painter"]:
            yield event.plain_result("❌ 只有画家可以使用此命令查看提示。")
            return

        # 给画家看额外提示（比如卡牌描述、攻击力等）
        code = game["code"]
        card = self.cards_db.get(code, {})
        extra = []
        if card.get("ability"):
            extra.append(f"能力：{card['ability']}")
        if card.get("attack") is not None and card.get("health") is not None:
            extra.append(f"身材：{card['attack']}/{card['health']}")
        
        if extra:
            yield event.plain_result(f"🔎 额外提示（仅画家可见）：\n" + "\n".join(extra))
        else:
            yield event.plain_result("ℹ️ 这张卡没有更多额外属性了，加油画吧！")

    @filter.command("我的关键词")
    async def get_my_keyword(self, event: AstrMessageEvent):
        '''私聊获取当前画猜关键词（私聊专用）
        用法：私聊机器人发送 /我的关键词'''
        # 这个命令设计为私聊使用，但也可以在群里用（此时会尝试私聊）
        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        
        # 查找用户所在群是否有游戏且该用户是画家
        found = None
        for gid, game in self.games.items():
            if game["painter"] == user_id:
                found = game
                break
        
        if not found:
            yield event.plain_result("❌ 你目前不是任何画猜游戏的画家。")
            return

        # 私聊发送关键词
        yield self.context.send_message_to_user(
            user_id,
            f"🎨 你当前的画猜关键词是：【{found['answer']}】\n请在群里画出来让大家猜吧！"
        )
        yield event.plain_result("✅ 关键词已通过私聊发送给你。")

    # 监听群消息，用于猜答案（注意：不能再用 @filter.command，否则会干扰命令解析）
    # 这里我们使用一个通用的消息处理钩子，但 AstrBot 中我们可以在类里重写 handle_message 或使用监听器。
    # 由于 AstrBot 支持 @filter 监听所有消息，但我们不想让命令重复触发，我们用另一种方式：
    # 在 AstrBot 中，最佳实践是创建一个独立的 listener，但为了单文件简洁，我们利用 event 的过滤。
    # 因为 AstrBot 的 @filter.command 只匹配命令，普通消息不触发。
    # 我们需要一个能抓取所有群消息的监听器。
    
    # 我们通过重写 Star 的 handle_message 方法？但 AstrBot 3.x 通常使用 filter 装饰器。
    # 我们可以这样：添加一个监听所有消息的方法，并手动过滤掉命令。
    # 但为了减少干扰，我们可以要求猜词时必须带特定前缀，比如 /猜 卡牌名。
    # 如果直接发消息猜，容易误触。结合你之前的需求，我建议猜词格式为：/猜 卡牌名
    # 这样可以完美避开干扰，而且用户也容易操作。
    
    @filter.command("猜")
    async def guess_card(self, event: AstrMessageEvent):
        '''猜测卡牌名称（必须在画猜游戏进行中）
        用法：/猜 <卡牌名>
        示例：/猜 闪电战'''
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令。")
            return

        if group_id not in self.games:
            yield event.plain_result("❌ 本群当前没有进行中的画猜游戏。")
            return

        if not event.message_str:
            yield event.plain_result("❌ 请输入你要猜的卡牌名。\n用法：/猜 卡牌名")
            return

        # 提取猜的词
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("❌ 请输入你要猜的卡牌名。\n示例：/猜 闪电战")
            return
        
        guess = parts[1].strip()
        game = self.games[group_id]
        answer = game["answer"]

        # 判断是否猜中（忽略大小写/空格，中文直接比较）
        if guess == answer:
            # 猜中了！
            winner = event.get_sender_name()
            painter_name = game["painter_name"]
            self.games.pop(group_id)  # 结束游戏
            
            yield event.plain_result(
                f"🎉 **恭喜 {winner} 猜对了！**\n"
                f"正确答案就是：【{answer}】\n"
                f"👏 画家 {painter_name} 画得真棒！"
            )
        else:
            yield event.plain_result(f"❌ 不对哦，再想想～（提示：{game['nation']}，{game['cost']}费）")

    async def terminate(self):
        """插件卸载时清理"""
        logger.info("KARDS你画我猜插件已卸载")
