import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
import asyncio
import time
import random
from typing import Optional, List, Tuple, Dict
from datetime import datetime
import os

# Конфигурация уровней сложности
DIFFICULTIES = {
    "легкий": {"width": 10, "height": 10, "mines": 15, "emoji": "🟢"},
    "средний": {"width": 15, "height": 15, "mines": 40, "emoji": "🟡"},
    "сложный": {"width": 20, "height": 20, "mines": 80, "emoji": "🔴"}
}

# Размер одного блока в сообщении (5×5 = 25 кнопок)
BLOCK_SIZE = 5

# Эмодзи для игры
EMOJI_HIDDEN = "⬛"
EMOJI_FLAG = "🚩"
EMOJI_MINE = "💣"
EMOJI_NUMBERS = {
    0: "⬜",
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣"
}

class MinesweeperGame:
    def __init__(self, width: int, height: int, mines: int, difficulty: str, players: List[int]):
        self.width = width
        self.height = height
        self.mines_count = mines
        self.difficulty = difficulty
        self.players = players
        self.is_coop = len(players) > 1
        
        # Игровая логика - ЕДИНОЕ хранилище
        self.board: List[List[int]] = []  # -1 = мина, 0-8 = количество мин вокруг
        self.revealed: List[List[bool]] = []
        self.flags: Dict[int, List[List[bool]]] = {pid: [[False] * width for _ in range(height)] for pid in players}
        self.flags_remaining: Dict[int, int] = {pid: mines for pid in players}
        
        self.started = False
        self.finished = False
        self.won = False
        self.start_time = None
        self.end_time = None
        self.flag_mode: Dict[int, bool] = {pid: False for pid in players}
        
        # Словарь для хранения ID сообщений блоков
        self.block_messages: Dict[Tuple[int, int], int] = {}  # (block_x, block_y) -> message_id
        
        self._generate_board()
    
    def _generate_board(self):
        """Генерирует игровое поле с минами"""
        self.board = [[0] * self.width for _ in range(self.height)]
        self.revealed = [[False] * self.width for _ in range(self.height)]
        
        # Размещаем мины
        mines_placed = 0
        while mines_placed < self.mines_count:
            x = random.randint(0, self.width - 1)
            y = random.randint(0, self.height - 1)
            if self.board[y][x] != -1:
                self.board[y][x] = -1
                mines_placed += 1
        
        # Подсчитываем числа
        for y in range(self.height):
            for x in range(self.width):
                if self.board[y][x] != -1:
                    self.board[y][x] = self._count_adjacent_mines(x, y)
    
    def _count_adjacent_mines(self, x: int, y: int) -> int:
        """Подсчитывает количество мин вокруг клетки"""
        count = 0
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dy == 0 and dx == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.width and 0 <= ny < self.height:
                    if self.board[ny][nx] == -1:
                        count += 1
        return count
    
    def reveal(self, x: int, y: int, player_id: int) -> Tuple[bool, List[Tuple[int, int]]]:
        """
        Открывает клетку. 
        Возвращает (game_continues, affected_blocks)
        affected_blocks - список блоков (block_x, block_y), которые нужно обновить
        """
        if not self.started:
            self.started = True
            self.start_time = time.time()
        
        if self.finished or self.revealed[y][x]:
            return True, []
        
        # Проверяем все флаги
        for pid in self.players:
            if self.flags[pid][y][x]:
                return True, []
        
        affected_blocks = set()
        self.revealed[y][x] = True
        affected_blocks.add((x // BLOCK_SIZE, y // BLOCK_SIZE))
        
        # Попали на мину
        if self.board[y][x] == -1:
            self.finished = True
            self.won = False
            self.end_time = time.time()
            # При проигрыше обновляем все блоки
            for bx in range((self.width + BLOCK_SIZE - 1) // BLOCK_SIZE):
                for by in range((self.height + BLOCK_SIZE - 1) // BLOCK_SIZE):
                    affected_blocks.add((bx, by))
            return False, list(affected_blocks)
        
        # Автоматическое открытие пустых клеток (flood fill)
        if self.board[y][x] == 0:
            flood_blocks = self._reveal_empty(x, y)
            affected_blocks.update(flood_blocks)
        
        # Проверка победы
        if self._check_win():
            self.finished = True
            self.won = True
            self.end_time = time.time()
            # При победе обновляем все блоки
            for bx in range((self.width + BLOCK_SIZE - 1) // BLOCK_SIZE):
                for by in range((self.height + BLOCK_SIZE - 1) // BLOCK_SIZE):
                    affected_blocks.add((bx, by))
        
        return True, list(affected_blocks)
    
    def _reveal_empty(self, x: int, y: int) -> set:
        """
        Рекурсивно открывает пустые клетки (BFS для производительности)
        Возвращает множество затронутых блоков
        """
        affected_blocks = set()
        queue = [(x, y)]
        visited = {(x, y)}
        
        while queue:
            cx, cy = queue.pop(0)
            affected_blocks.add((cx // BLOCK_SIZE, cy // BLOCK_SIZE))
            
            for dy in [-1, 0, 1]:
                for dx in [-1, 0, 1]:
                    if dy == 0 and dx == 0:
                        continue
                    
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        if (nx, ny) not in visited:
                            visited.add((nx, ny))
                            
                            # Проверяем флаги
                            has_flag = any(self.flags[pid][ny][nx] for pid in self.players)
                            
                            if not self.revealed[ny][nx] and not has_flag:
                                self.revealed[ny][nx] = True
                                affected_blocks.add((nx // BLOCK_SIZE, ny // BLOCK_SIZE))
                                
                                # Продолжаем flood fill только для пустых клеток
                                if self.board[ny][nx] == 0:
                                    queue.append((nx, ny))
        
        return affected_blocks
    
    def toggle_flag(self, x: int, y: int, player_id: int) -> Tuple[bool, Tuple[int, int]]:
        """
        Переключает флаг. 
        Возвращает (success, affected_block)
        """
        if self.finished or self.revealed[y][x]:
            return False, None
        
        if self.flags[player_id][y][x]:
            self.flags[player_id][y][x] = False
            self.flags_remaining[player_id] += 1
        else:
            if self.flags_remaining[player_id] > 0:
                self.flags[player_id][y][x] = True
                self.flags_remaining[player_id] -= 1
            else:
                return False, None
        
        return True, (x // BLOCK_SIZE, y // BLOCK_SIZE)
    
    def _check_win(self) -> bool:
        """Проверяет условие победы"""
        for y in range(self.height):
            for x in range(self.width):
                if self.board[y][x] != -1 and not self.revealed[y][x]:
                    return False
        return True
    
    def get_time(self) -> float:
        """Возвращает время игры в секундах"""
        if not self.started:
            return 0.0
        if self.finished:
            return round(self.end_time - self.start_time, 2)
        return round(time.time() - self.start_time, 2)
    
    def get_cell_emoji(self, x: int, y: int) -> str:
        """Возвращает эмодзи для клетки"""
        # Проверяем флаги всех игроков
        for i, pid in enumerate(self.players):
            if self.flags[pid][y][x]:
                # Разные цифры для разных игроков в кооп режиме
                if self.is_coop:
                    return f"{i+1}️⃣"
                return EMOJI_FLAG
        
        if not self.revealed[y][x]:
            return EMOJI_HIDDEN
        
        if self.board[y][x] == -1:
            return EMOJI_MINE
        
        return EMOJI_NUMBERS[self.board[y][x]]

class CellButton(discord.ui.Button):
    def __init__(self, game: MinesweeperGame, x: int, y: int, bot):
        self.game = game
        self.x = x
        self.y = y
        self.bot = bot
        
        # Получаем эмодзи для кнопки
        emoji = game.get_cell_emoji(x, y)
        
        # Определяем стиль
        if game.revealed[y][x]:
            if game.board[y][x] == -1:
                style = discord.ButtonStyle.danger
            else:
                style = discord.ButtonStyle.secondary
        else:
            style = discord.ButtonStyle.primary
        
        super().__init__(
            style=style,
            emoji=emoji,
            custom_id=f"cell_{x}_{y}",
            row=y % BLOCK_SIZE
        )
    
    async def callback(self, interaction: discord.Interaction):
        # Проверка прав
        if interaction.user.id not in self.game.players:
            await interaction.response.send_message("Вы не участвуете в этой игре!", ephemeral=True)
            return
        
        if self.game.finished:
            await interaction.response.send_message("Игра завершена!", ephemeral=True)
            return
        
        player_id = interaction.user.id
        
        # Определяем действие
        if self.game.flag_mode.get(player_id, False):
            # Режим флага
            success, affected_block = self.game.toggle_flag(self.x, self.y, player_id)
            if not success:
                await interaction.response.send_message("Нельзя поставить флаг здесь!", ephemeral=True)
                return
            
            # Обновляем только один блок
            await interaction.response.defer()
            await self.bot.update_game_blocks(self.game, [affected_block])
        else:
            # Режим копания
            continue_game, affected_blocks = self.game.reveal(self.x, self.y, player_id)
            
            # Сохраняем результат если игра закончилась
            if self.game.finished:
                await self.bot.save_game_result(self.game)
            
            # Обновляем все затронутые блоки
            await interaction.response.defer()
            await self.bot.update_game_blocks(self.game, affected_blocks)

class BlockView(discord.ui.View):
    def __init__(self, game: MinesweeperGame, block_x: int, block_y: int, bot):
        super().__init__(timeout=None)
        self.game = game
        self.block_x = block_x
        self.block_y = block_y
        self.bot = bot
        
        # Добавляем кнопки для клеток в этом блоке
        start_x = block_x * BLOCK_SIZE
        start_y = block_y * BLOCK_SIZE
        
        for dy in range(BLOCK_SIZE):
            for dx in range(BLOCK_SIZE):
                x = start_x + dx
                y = start_y + dy
                
                # Проверяем что клетка в пределах поля
                if x < game.width and y < game.height:
                    button = CellButton(game, x, y, bot)
                    self.add_item(button)

class ControlView(discord.ui.View):
    def __init__(self, game: MinesweeperGame, bot):
        super().__init__(timeout=None)
        self.game = game
        self.bot = bot
    
    @discord.ui.button(label="Режим флага", style=discord.ButtonStyle.secondary, emoji="🚩", custom_id="toggle_flag", row=0)
    async def toggle_flag_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.game.players:
            await interaction.response.send_message("Вы не участвуете в этой игре!", ephemeral=True)
            return
        
        if self.game.finished:
            await interaction.response.send_message("Игра завершена!", ephemeral=True)
            return
        
        player_id = interaction.user.id
        self.game.flag_mode[player_id] = not self.game.flag_mode[player_id]
        mode = "установки флагов 🚩" if self.game.flag_mode[player_id] else "копания ⛏️"
        
        await interaction.response.send_message(f"Режим: **{mode}**", ephemeral=True)
        
        # Обновляем информационное сообщение
        await self.bot.update_game_info(self.game)
    
    @discord.ui.button(label="Сдаться", style=discord.ButtonStyle.danger, emoji="🏳️", custom_id="surrender", row=0)
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.game.players:
            await interaction.response.send_message("Вы не участвуете в этой игре!", ephemeral=True)
            return
        
        if self.game.finished:
            await interaction.response.send_message("Игра уже завершена!", ephemeral=True)
            return
        
        # Завершаем игру
        self.game.finished = True
        self.game.won = False
        if self.game.started:
            self.game.end_time = time.time()
        
        # Сохраняем поражение
        await self.bot.save_game_result(self.game)
        
        # Обновляем все блоки
        all_blocks = []
        for bx in range((self.game.width + BLOCK_SIZE - 1) // BLOCK_SIZE):
            for by in range((self.game.height + BLOCK_SIZE - 1) // BLOCK_SIZE):
                all_blocks.append((bx, by))
        
        await interaction.response.defer()
        await self.bot.update_game_blocks(self.game, all_blocks)
        await self.bot.update_game_info(self.game)

class LeaderboardView(discord.ui.View):
    def __init__(self, bot, difficulty: str, mode: str = "time"):
        super().__init__(timeout=180)
        self.bot = bot
        self.difficulty = difficulty
        self.mode = mode
        self.page = 0
        self.is_coop = False
    
    @discord.ui.button(label="⏱️ По времени", style=discord.ButtonStyle.primary, custom_id="mode_time")
    async def mode_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "time"
        self.page = 0
        await interaction.response.defer()
        await self.update_leaderboard(interaction)
    
    @discord.ui.button(label="📊 По винрейту", style=discord.ButtonStyle.primary, custom_id="mode_winrate")
    async def mode_winrate(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "winrate"
        self.page = 0
        await interaction.response.defer()
        await self.update_leaderboard(interaction)
    
    @discord.ui.button(label="👥 Кооп", style=discord.ButtonStyle.secondary, custom_id="toggle_coop")
    async def toggle_coop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.is_coop = not self.is_coop
        self.page = 0
        await interaction.response.defer()
        await self.update_leaderboard(interaction)
    
    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.defer()
            await self.update_leaderboard(interaction)
        else:
            await interaction.response.send_message("Это первая страница!", ephemeral=True)
    
    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.defer()
        await self.update_leaderboard(interaction)
    
    async def update_leaderboard(self, interaction: discord.Interaction):
        embed = await self.get_leaderboard_embed()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def get_leaderboard_embed(self) -> discord.Embed:
        difficulty_emoji = DIFFICULTIES[self.difficulty]["emoji"]
        
        if self.is_coop:
            title = f"🏆 Таблица лидеров (Кооп) {difficulty_emoji}"
        else:
            title = f"🏆 Таблица лидеров {difficulty_emoji}"
        
        if self.mode == "time":
            title += " - По времени"
            color = discord.Color.gold()
        else:
            title += " - По винрейту"
            color = discord.Color.blue()
        
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="Сложность", value=self.difficulty.capitalize(), inline=True)
        
        # Получаем данные из БД
        if self.is_coop:
            if self.mode == "time":
                leaders = await self.bot.db.get_coop_time_leaderboard(self.difficulty, self.page * 10, 10)
            else:
                leaders = await self.bot.db.get_coop_winrate_leaderboard(self.difficulty, self.page * 10, 10)
        else:
            if self.mode == "time":
                leaders = await self.bot.db.get_time_leaderboard(self.difficulty, self.page * 10, 10)
            else:
                leaders = await self.bot.db.get_winrate_leaderboard(self.difficulty, self.page * 10, 10)
        
        if not leaders:
            embed.description = "Пока нет записей в таблице лидеров!"
            return embed
        
        # Формируем таблицу
        lines = []
        for i, leader in enumerate(leaders, start=self.page * 10 + 1):
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            
            if self.is_coop:
                user1 = self.bot.get_user(leader['player1_id'])
                user2 = self.bot.get_user(leader['player2_id'])
                name1 = user1.display_name if user1 else f"User {leader['player1_id']}"
                name2 = user2.display_name if user2 else f"User {leader['player2_id']}"
                player_name = f"{name1} & {name2}"
            else:
                user = self.bot.get_user(leader['player_id'])
                player_name = user.display_name if user else f"User {leader['player_id']}"
            
            if self.mode == "time":
                value = f"{leader['best_time']:.2f}с"
                stats = f"W:{leader['wins']}"
            else:
                winrate = (leader['wins'] / leader['total_games'] * 100) if leader['total_games'] > 0 else 0
                value = f"{winrate:.1f}%"
                stats = f"W:{leader['wins']} L:{leader['total_games'] - leader['wins']}"
            
            lines.append(f"{medal} **{player_name}** - {value} ({stats})")
        
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Страница {self.page + 1}")
        
        return embed

class Database:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.pool = None
    
    async def connect(self):
        self.pool = await asyncpg.create_pool(self.connection_string, min_size=2, max_size=10)
        await self.create_tables()
    
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS solo_games (
                    id SERIAL PRIMARY KEY,
                    player_id BIGINT NOT NULL,
                    difficulty VARCHAR(20) NOT NULL,
                    won BOOLEAN NOT NULL,
                    time FLOAT NOT NULL,
                    played_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_solo_player_difficulty 
                ON solo_games(player_id, difficulty)
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS coop_games (
                    id SERIAL PRIMARY KEY,
                    player1_id BIGINT NOT NULL,
                    player2_id BIGINT NOT NULL,
                    difficulty VARCHAR(20) NOT NULL,
                    won BOOLEAN NOT NULL,
                    time FLOAT NOT NULL,
                    played_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_coop_players_difficulty 
                ON coop_games(player1_id, player2_id, difficulty)
            ''')
    
    async def save_solo_game(self, player_id: int, difficulty: str, won: bool, time: float):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO solo_games (player_id, difficulty, won, time)
                VALUES ($1, $2, $3, $4)
            ''', player_id, difficulty, won, time)
    
    async def save_coop_game(self, player1_id: int, player2_id: int, difficulty: str, won: bool, time: float):
        p1, p2 = sorted([player1_id, player2_id])
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO coop_games (player1_id, player2_id, difficulty, won, time)
                VALUES ($1, $2, $3, $4, $5)
            ''', p1, p2, difficulty, won, time)
    
    async def get_player_stats(self, player_id: int):
        async with self.pool.acquire() as conn:
            stats = {}
            for diff in DIFFICULTIES.keys():
                row = await conn.fetchrow('''
                    SELECT 
                        COUNT(*) as total_games,
                        SUM(CASE WHEN won THEN 1 ELSE 0 END) as wins,
                        MIN(CASE WHEN won THEN time END) as best_time
                    FROM solo_games
                    WHERE player_id = $1 AND difficulty = $2
                ''', player_id, diff)
                stats[diff] = dict(row) if row else {'total_games': 0, 'wins': 0, 'best_time': None}
            return stats
    
    async def get_time_leaderboard(self, difficulty: str, offset: int, limit: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT 
                    player_id,
                    MIN(time) as best_time,
                    SUM(CASE WHEN won THEN 1 ELSE 0 END) as wins,
                    COUNT(*) as total_games
                FROM solo_games
                WHERE difficulty = $1 AND won = true
                GROUP BY player_id
                ORDER BY best_time ASC
                LIMIT $2 OFFSET $3
            ''', difficulty, limit, offset)
            return [dict(row) for row in rows]
    
    async def get_winrate_leaderboard(self, difficulty: str, offset: int, limit: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT 
                    player_id,
                    SUM(CASE WHEN won THEN 1 ELSE 0 END) as wins,
                    COUNT(*) as total_games,
                    CAST(SUM(CASE WHEN won THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as winrate
                FROM solo_games
                WHERE difficulty = $1
                GROUP BY player_id
                HAVING COUNT(*) >= 5
                ORDER BY winrate DESC, wins DESC
                LIMIT $2 OFFSET $3
            ''', difficulty, limit, offset)
            return [dict(row) for row in rows]
    
    async def get_coop_time_leaderboard(self, difficulty: str, offset: int, limit: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT 
                    player1_id, player2_id,
                    MIN(time) as best_time,
                    SUM(CASE WHEN won THEN 1 ELSE 0 END) as wins,
                    COUNT(*) as total_games
                FROM coop_games
                WHERE difficulty = $1 AND won = true
                GROUP BY player1_id, player2_id
                ORDER BY best_time ASC
                LIMIT $2 OFFSET $3
            ''', difficulty, limit, offset)
            return [dict(row) for row in rows]
    
    async def get_coop_winrate_leaderboard(self, difficulty: str, offset: int, limit: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT 
                    player1_id, player2_id,
                    SUM(CASE WHEN won THEN 1 ELSE 0 END) as wins,
                    COUNT(*) as total_games,
                    CAST(SUM(CASE WHEN won THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as winrate
                FROM coop_games
                WHERE difficulty = $1
                GROUP BY player1_id, player2_id
                HAVING COUNT(*) >= 5
                ORDER BY winrate DESC, wins DESC
                LIMIT $2 OFFSET $3
            ''', difficulty, limit, offset)
            return [dict(row) for row in rows]

class MinesweeperBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.active_games: Dict[int, MinesweeperGame] = {}
        self.game_messages: Dict[int, Dict[str, int]] = {}  # player_id -> {"info": msg_id, "blocks": {...}}
        self.db = None
    
    async def setup_hook(self):
        db_url = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/minesweeper")
        self.db = Database(db_url)
        await self.db.connect()
        await self.tree.sync()
        print(f"Бот {self.user} готов!")
    
    async def update_game_blocks(self, game: MinesweeperGame, affected_blocks: List[Tuple[int, int]]):
        """Обновляет только затронутые блоки игры"""
        update_tasks = []
        
        for block_x, block_y in affected_blocks:
            if (block_x, block_y) in game.block_messages:
                msg_id = game.block_messages[(block_x, block_y)]
                update_tasks.append(self._update_single_block(game, block_x, block_y, msg_id))
        
        # Параллельное обновление для скорости
        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)
    
    async def _update_single_block(self, game: MinesweeperGame, block_x: int, block_y: int, msg_id: int):
        """Обновляет один блок"""
        try:
            # Находим сообщение и канал
            for player_id in game.players:
                if player_id in self.game_messages:
                    channel_id = self.game_messages[player_id].get("channel_id")
                    if channel_id:
                        channel = self.get_channel(channel_id)
                        if channel:
                            try:
                                message = await channel.fetch_message(msg_id)
                                view = BlockView(game, block_x, block_y, self)
                                await message.edit(view=view)
                                return
                            except:
                                pass
        except Exception as e:
            print(f"Ошибка обновления блока ({block_x}, {block_y}): {e}")
    
    async def update_game_info(self, game: MinesweeperGame):
        """Обновляет информационное сообщение"""
        try:
            for player_id in game.players:
                if player_id in self.game_messages:
                    info_msg_id = self.game_messages[player_id].get("info")
                    channel_id = self.game_messages[player_id].get("channel_id")
                    
                    if info_msg_id and channel_id:
                        channel = self.get_channel(channel_id)
                        if channel:
                            try:
                                message = await channel.fetch_message(info_msg_id)
                                embed = self.create_info_embed(game)
                                view = ControlView(game, self)
                                await message.edit(embed=embed, view=view)
                            except:
                                pass
        except Exception as e:
            print(f"Ошибка обновления информации: {e}")
    
    def create_info_embed(self, game: MinesweeperGame) -> discord.Embed:
        """Создает информационный embed"""
        difficulty_emoji = DIFFICULTIES[game.difficulty]["emoji"]
        
        if game.finished:
            if game.won:
                title = f"🎉 ПОБЕДА! {difficulty_emoji}"
                color = discord.Color.green()
            else:
                title = f"💥 ПОРАЖЕНИЕ {difficulty_emoji}"
                color = discord.Color.red()
        else:
            title = f"⛏️ Сапёр - {game.difficulty.capitalize()} {difficulty_emoji}"
            color = discord.Color.blue()
        
        embed = discord.Embed(title=title, color=color)
        
        # Информация об игроках
        players_info = []
        for pid in game.players:
            user = self.get_user(pid)
            username = user.display_name if user else f"User {pid}"
            flag_emoji = "🚩" if game.flag_mode.get(pid, False) else "⛏️"
            players_info.append(f"{flag_emoji} **{username}**: {game.flags_remaining[pid]} 🚩")
        
        embed.add_field(name="Игроки", value="\n".join(players_info), inline=False)
        
        # Статистика
        embed.add_field(name="⏱️ Время", value=f"{game.get_time():.2f} сек", inline=True)
        embed.add_field(name="📐 Поле", value=f"{game.width}×{game.height}", inline=True)
        embed.add_field(name="💣 Мины", value=f"{game.mines_count}", inline=True)
        
        # Инструкция
        if not game.finished:
            embed.set_footer(text="Нажимайте на клетки ниже! Используйте кнопку 🚩 для переключения режима.")
        else:
            if game.won:
                embed.set_footer(text=f"Игра завершена за {game.get_time():.2f} секунд!")
            else:
                embed.set_footer(text="Попробуйте ещё раз!")
        
        return embed
    
    async def save_game_result(self, game: MinesweeperGame):
        """Сохраняет результат игры в БД"""
        if not game.started:
            return
        
        game_time = game.get_time()
        
        try:
            if game.is_coop:
                await self.db.save_coop_game(
                    game.players[0],
                    game.players[1],
                    game.difficulty,
                    game.won,
                    game_time
                )
            else:
                await self.db.save_solo_game(
                    game.players[0],
                    game.difficulty,
                    game.won,
                    game_time
                )
        except Exception as e:
            print(f"Ошибка сохранения игры: {e}")

bot = MinesweeperBot()

@bot.tree.command(name="сапёр", description="Запустить игру в сапёр")
@app_commands.describe(сложность="Выберите уровень сложности")
@app_commands.choices(сложность=[
    app_commands.Choice(name="🟢 Легкий (10×10, 15 мин)", value="легкий"),
    app_commands.Choice(name="🟡 Средний (15×15, 40 мин)", value="средний"),
    app_commands.Choice(name="🔴 Сложный (20×20, 80 мин)", value="сложный")
])
async def minesweeper(interaction: discord.Interaction, сложность: str):
    """Запускает новую игру в сапёр"""
    
    if interaction.user.id in bot.active_games:
        await interaction.response.send_message(
            "У вас уже есть активная игра! Используйте кнопку 🏳️ Сдаться для завершения.",
            ephemeral=True
        )
        return
    
    await interaction.response.defer()
    
    # Создаем игру
    config = DIFFICULTIES[сложность]
    game = MinesweeperGame(
        config["width"],
        config["height"],
        config["mines"],
        сложность,
        [interaction.user.id]
    )
    
    bot.active_games[interaction.user.id] = game
    bot.game_messages[interaction.user.id] = {"channel_id": interaction.channel_id}
    
    # Отправляем информационное сообщение с управлением
    embed = bot.create_info_embed(game)
    view = ControlView(game, bot)
    info_msg = await interaction.followup.send(embed=embed, view=view)
    bot.game_messages[interaction.user.id]["info"] = info_msg.id
    
    # Создаем блоки игрового поля
    blocks_x = (game.width + BLOCK_SIZE - 1) // BLOCK_SIZE
    blocks_y = (game.height + BLOCK_SIZE - 1) // BLOCK_SIZE
    
    for by in range(blocks_y):
        for bx in range(blocks_x):
            view = BlockView(game, bx, by, bot)
            
            # Заголовок блока
            start_x = bx * BLOCK_SIZE
            start_y = by * BLOCK_SIZE
            end_x = min(start_x + BLOCK_SIZE - 1, game.width - 1)
            end_y = min(start_y + BLOCK_SIZE - 1, game.height - 1)
            
            block_msg = await interaction.channel.send(
                f"**Блок ({start_x}-{end_x}, {start_y}-{end_y})**",
                view=view
            )
            
            game.block_messages[(bx, by)] = block_msg.id
    
    await interaction.channel.send("✅ Игра создана! Кликайте по клеткам выше для игры.")

@bot.tree.command(name="кооп", description="Запустить кооперативную игру в сапёр")
@app_commands.describe(
    партнёр="Выберите партнёра для игры",
    сложность="Выберите уровень сложности"
)
@app_commands.choices(сложность=[
    app_commands.Choice(name="🟢 Легкий (10×10, 15 мин)", value="легкий"),
    app_commands.Choice(name="🟡 Средний (15×15, 40 мин)", value="средний"),
    app_commands.Choice(name="🔴 Сложный (20×20, 80 мин)", value="сложный")
])
async def coop(interaction: discord.Interaction, партнёр: discord.User, сложность: str):
    """Запускает кооперативную игру"""
    
    if партнёр.bot:
        await interaction.response.send_message("Нельзя играть с ботом!", ephemeral=True)
        return
    
    if партнёр.id == interaction.user.id:
        await interaction.response.send_message("Нельзя играть с самим собой!", ephemeral=True)
        return
    
    if interaction.user.id in bot.active_games or партнёр.id in bot.active_games:
        await interaction.response.send_message(
            "У одного из игроков уже есть активная игра!",
            ephemeral=True
        )
        return
    
    await interaction.response.defer()
    
    # Создаем кооперативную игру
    config = DIFFICULTIES[сложность]
    game = MinesweeperGame(
        config["width"],
        config["height"],
        config["mines"],
        сложность,
        [interaction.user.id, партнёр.id]
    )
    
    bot.active_games[interaction.user.id] = game
    bot.active_games[партнёр.id] = game
    bot.game_messages[interaction.user.id] = {"channel_id": interaction.channel_id}
    bot.game_messages[партнёр.id] = {"channel_id": interaction.channel_id}
    
    # Информационное сообщение
    embed = bot.create_info_embed(game)
    view = ControlView(game, bot)
    info_msg = await interaction.followup.send(
        content=f"🤝 Кооперативная игра: {interaction.user.mention} и {партнёр.mention}",
        embed=embed,
        view=view
    )
    bot.game_messages[interaction.user.id]["info"] = info_msg.id
    bot.game_messages[партнёр.id]["info"] = info_msg.id
    
    # Создаем блоки
    blocks_x = (game.width + BLOCK_SIZE - 1) // BLOCK_SIZE
    blocks_y = (game.height + BLOCK_SIZE - 1) // BLOCK_SIZE
    
    for by in range(blocks_y):
        for bx in range(blocks_x):
            view = BlockView(game, bx, by, bot)
            
            start_x = bx * BLOCK_SIZE
            start_y = by * BLOCK_SIZE
            end_x = min(start_x + BLOCK_SIZE - 1, game.width - 1)
            end_y = min(start_y + BLOCK_SIZE - 1, game.height - 1)
            
            block_msg = await interaction.channel.send(
                f"**Блок ({start_x}-{end_x}, {start_y}-{end_y})**",
                view=view
            )
            
            game.block_messages[(bx, by)] = block_msg.id
    
    await interaction.channel.send("✅ Кооперативная игра создана!")

@bot.tree.command(name="таблица_лидеров", description="Показать таблицу лидеров")
@app_commands.describe(сложность="Выберите уровень сложности")
@app_commands.choices(сложность=[
    app_commands.Choice(name="🟢 Легкий", value="легкий"),
    app_commands.Choice(name="🟡 Средний", value="средний"),
    app_commands.Choice(name="🔴 Сложный", value="сложный")
])
async def leaderboard(interaction: discord.Interaction, сложность: str):
    """Показывает таблицу лидеров"""
    view = LeaderboardView(bot, сложность)
    embed = await view.get_leaderboard_embed()
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="профиль", description="Показать свою статистику")
@app_commands.describe(игрок="Посмотреть профиль другого игрока")
async def profile(interaction: discord.Interaction, игрок: Optional[discord.User] = None):
    """Показывает профиль игрока"""
    target_user = игрок if игрок else interaction.user
    stats = await bot.db.get_player_stats(target_user.id)
    
    embed = discord.Embed(
        title=f"📊 Профиль: {target_user.display_name}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=target_user.display_avatar.url)
    
    for diff, diff_stats in stats.items():
        emoji = DIFFICULTIES[diff]["emoji"]
        total = diff_stats['total_games']
        wins = diff_stats['wins']
        best_time = diff_stats['best_time']
        
        if total == 0:
            value = "Игр не сыграно"
        else:
            winrate = (wins / total * 100) if total > 0 else 0
            time_str = f"{best_time:.2f}с" if best_time else "—"
            value = f"🎮 Игр: {total}\n🏆 Побед: {wins} ({winrate:.1f}%)\n⏱️ Рекорд: {time_str}"
        
        embed.add_field(name=f"{emoji} {diff.capitalize()}", value=value, inline=True)
    
    total_all = sum(s['total_games'] for s in stats.values())
    wins_all = sum(s['wins'] for s in stats.values())
    
    if total_all > 0:
        overall_winrate = (wins_all / total_all * 100)
        embed.add_field(
            name="📈 Общая статистика",
            value=f"Всего игр: {total_all}\nПобед: {wins_all}\nВинрейт: {overall_winrate:.1f}%",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="правила", description="Показать правила игры")
async def rules(interaction: discord.Interaction):
    """Показывает правила игры"""
    embed = discord.Embed(
        title="📖 Правила игры в Сапёр",
        description="Цель — открыть все клетки, не наткнувшись на мину!",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🎮 Как играть",
        value=(
            "• Нажимайте **🚩 Режим флага** для переключения режимов\n"
            "• **Режим копания** ⛏️: открывает клетки\n"
            "• **Режим флага** 🚩: помечает подозрительные клетки\n"
            "• Числа показывают мины вокруг клетки\n"
            "• Откройте все безопасные клетки для победы!"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🎯 Уровни сложности",
        value=(
            "🟢 **Легкий**: 10×10, 15 мин (2×2 блока)\n"
            "🟡 **Средний**: 15×15, 40 мин (3×3 блока)\n"
            "🔴 **Сложный**: 20×20, 80 мин (4×4 блока)"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🤝 Кооп",
        value=(
            "• Играйте вдвоём!\n"
            "• У каждого свои флаги (1️⃣ и 2️⃣)\n"
            "• Отдельная таблица лидеров\n"
            "• `/кооп @партнёр сложность`"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚡ Команды",
        value=(
            "`/сапёр` — соло игра\n"
            "`/кооп` — игра вдвоём\n"
            "`/таблица_лидеров` — рейтинги\n"
            "`/профиль` — статистика\n"
            "`/правила` — эта справка"
        ),
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ Не найден DISCORD_TOKEN!")
        print("Установите: export DISCORD_TOKEN='ваш_токен'")
        exit(1)
    
    bot.run(TOKEN)
