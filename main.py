import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import os
import random
import time
from typing import Optional, List, Tuple
from datetime import datetime, timedelta
import asyncio
from aiohttp import web

# Конфигурация
DATABASE_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
PORT = int(os.getenv('PORT', 10000))

intents = discord.Intents.default()
intents.message_content = True

class MinesweeperBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.db_pool = None
    
    async def setup_hook(self):
        await self.tree.sync()
        self.db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        await self.init_database()
    
    async def init_database(self):
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    total_blocks_cleared INTEGER DEFAULT 0,
                    total_time_spent FLOAT DEFAULT 0,
                    best_speed FLOAT DEFAULT 0,
                    games_played INTEGER DEFAULT 0,
                    best_blocks_normal INTEGER DEFAULT 0,
                    best_blocks_hardcore INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS active_games (
                    thread_id BIGINT PRIMARY KEY,
                    user_id BIGINT,
                    mode TEXT,
                    current_block INTEGER DEFAULT 0,
                    blocks_cleared INTEGER DEFAULT 0,
                    start_time TIMESTAMP,
                    last_action_time TIMESTAMP,
                    is_multiplayer BOOLEAN DEFAULT FALSE,
                    hardcore_timer FLOAT DEFAULT 0,
                    game_data JSONB,
                    FOREIGN KEY (user_id) REFERENCES players(user_id)
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS speed_leaderboard (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    avg_speed FLOAT,
                    total_blocks INTEGER,
                    total_time FLOAT,
                    last_updated TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES players(user_id)
                )
            ''')
            
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_players_best_speed ON players(best_speed DESC)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_leaderboard ON speed_leaderboard(avg_speed DESC)')

bot = MinesweeperBot()

class MinesweeperGame:
    def __init__(self, mode='normal', is_multiplayer=False):
        self.mode = mode
        self.is_multiplayer = is_multiplayer
        self.blocks_cleared = 0
        self.start_time = time.time()
        self.last_action_time = time.time()
        self.hardcore_timer = 30.0 if mode == 'hardcore' else 0
        self.grid = []  # Единая сетка 10x5 (два блока по вертикали)
        self.mines = set()
        self.cells_revealed = set()
        self.message_ids = []
        self.first_click = True
        
    def generate_field(self, difficulty_level=0):
        """Генерирует поле 10x5 (два блока 5x5 по вертикали)"""
        if self.mode == 'hardcore':
            base_mines = 5
            mines_count = min(base_mines + (difficulty_level // 3), 12)
        else:
            mines_count = 5
        
        # Создаем пустую сетку 10x5
        self.grid = [[0 for _ in range(5)] for _ in range(10)]
        self.mines = set()
        
        # Размещаем бомбы
        while len(self.mines) < mines_count:
            x, y = random.randint(0, 4), random.randint(0, 9)
            if (x, y) not in self.mines:
                self.mines.add((x, y))
                self.grid[y][x] = -1
        
        # Вычисляем числа для всей сетки
        for y in range(10):
            for x in range(5):
                if self.grid[y][x] != -1:
                    count = 0
                    for dy in [-1, 0, 1]:
                        for dx in [-1, 0, 1]:
                            ny, nx = y + dy, x + dx
                            if 0 <= ny < 10 and 0 <= nx < 5 and self.grid[ny][nx] == -1:
                                count += 1
                    self.grid[y][x] = count
    
    def ensure_safe_first_click(self, x, y):
        """Гарантирует, что первый клик безопасный"""
        if (x, y) in self.mines:
            # Перемещаем бомбу в другое место
            self.mines.remove((x, y))
            self.grid[y][x] = 0
            
            # Находим новое место для бомбы
            while True:
                new_x, new_y = random.randint(0, 4), random.randint(0, 9)
                if (new_x, new_y) not in self.mines and (new_x, new_y) != (x, y):
                    self.mines.add((new_x, new_y))
                    self.grid[new_y][new_x] = -1
                    break
            
            # Пересчитываем числа вокруг обеих клеток
            for cy, cx in [(y, x), (new_y, new_x)]:
                for dy in range(-1, 2):
                    for dx in range(-1, 2):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < 10 and 0 <= nx < 5 and self.grid[ny][nx] != -1:
                            count = 0
                            for ddy in [-1, 0, 1]:
                                for ddx in [-1, 0, 1]:
                                    nny, nnx = ny + ddy, nx + ddx
                                    if 0 <= nny < 10 and 0 <= nnx < 5 and self.grid[nny][nnx] == -1:
                                        count += 1
                            self.grid[ny][nx] = count
    
    def get_time_bonus_hardcore(self):
        base_bonus = 18
        reduction = (self.blocks_cleared // 5) * 1
        return max(5, base_bonus - reduction)
    
    def reveal_cell(self, x, y):
        """Открывает клетку"""
        if (x, y) in self.cells_revealed:
            return 'already_revealed', set()
        
        # Первый клик всегда безопасный
        if self.first_click:
            self.ensure_safe_first_click(x, y)
            self.first_click = False
        
        if self.grid[y][x] == -1:
            return 'mine', {(x, y)}
        
        # Flood fill
        revealed = set()
        stack = [(x, y)]
        
        while stack:
            cx, cy = stack.pop()
            if (cx, cy) in self.cells_revealed or (cx, cy) in revealed:
                continue
            
            revealed.add((cx, cy))
            
            if self.grid[cy][cx] == 0:
                for dy in [-1, 0, 1]:
                    for dx in [-1, 0, 1]:
                        nx, ny = cx + dx, cy + dy
                        if 0 <= ny < 10 and 0 <= nx < 5:
                            if (nx, ny) not in self.cells_revealed:
                                stack.append((nx, ny))
        
        return 'safe', revealed
    
    def is_field_complete(self):
        """Проверяет, пройдено ли всё поле"""
        total_safe_cells = 0
        revealed_safe_cells = 0
        
        for y in range(10):
            for x in range(5):
                if self.grid[y][x] != -1:
                    total_safe_cells += 1
                    if (x, y) in self.cells_revealed:
                        revealed_safe_cells += 1
        
        return revealed_safe_cells == total_safe_cells

class MinesweeperView(discord.ui.View):
    def __init__(self, game: MinesweeperGame, user_id: int, thread_id: int, block_idx: int):
        super().__init__(timeout=None)
        self.game = game
        self.user_id = user_id
        self.thread_id = thread_id
        self.block_idx = block_idx  # 0 или 1 (верхний или нижний блок)
        self.update_buttons()
    
    def update_buttons(self):
        self.clear_items()
        
        # Определяем диапазон Y для этого блока
        y_start = self.block_idx * 5
        y_end = y_start + 5
        
        for local_y in range(5):
            for x in range(5):
                global_y = y_start + local_y
                button = MinesweeperButton(x, global_y, self.game.grid[global_y][x], local_y)
                
                if (x, global_y) in self.game.cells_revealed:
                    button.disabled = True
                    value = self.game.grid[global_y][x]
                    if value == 0:
                        button.label = '◽'
                        button.style = discord.ButtonStyle.secondary
                    elif value == -1:
                        button.label = '💣'
                        button.style = discord.ButtonStyle.danger
                    else:
                        # Цветные числа как в настоящем сапёре
                        button.label = str(value)
                        if value == 1:
                            button.style = discord.ButtonStyle.primary
                        elif value == 2:
                            button.style = discord.ButtonStyle.success
                        else:
                            button.style = discord.ButtonStyle.danger
                
                self.add_item(button)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.game.is_multiplayer and interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваша игра!", ephemeral=True)
            return False
        return True

class MinesweeperButton(discord.ui.Button):
    def __init__(self, x: int, y: int, value: int, row: int):
        super().__init__(style=discord.ButtonStyle.secondary, label='⬜', row=row)
        self.x = x
        self.y = y
        self.cell_value = value
    
    async def callback(self, interaction: discord.Interaction):
        view: MinesweeperView = self.view
        game = view.game
        
        # Таймаут Discord - отвечаем быстро
        try:
            await interaction.response.defer()
        except:
            return
        
        game.last_action_time = time.time()
        
        result, revealed = game.reveal_cell(self.x, self.y)
        
        if result == 'already_revealed':
            return
        
        if result == 'mine':
            await self.handle_game_over(interaction, view)
            return
        
        game.cells_revealed.update(revealed)
        
        if game.is_field_complete():
            await self.handle_field_complete(interaction, view)
        else:
            # Обновляем оба блока
            await self.update_both_views(interaction, view)
    
    async def update_both_views(self, interaction: discord.Interaction, view: MinesweeperView):
        """Обновляет оба блока на экране"""
        thread = interaction.channel
        game = view.game
        
        try:
            # Обновляем оба сообщения
            for i, msg_id in enumerate(game.message_ids):
                try:
                    msg = await thread.fetch_message(msg_id)
                    new_view = MinesweeperView(game, view.user_id, view.thread_id, i)
                    
                    timer_text = ""
                    if game.mode == 'hardcore':
                        timer_text = f" | ⏱️ **{game.hardcore_timer:.1f}с**"
                    
                    safe_cells = len([1 for y in range(10) for x in range(5) if game.grid[y][x] != -1])
                    revealed = len(game.cells_revealed)
                    
                    await msg.edit(
                        content=f"🎮 **Блок {game.blocks_cleared + 1} - {'Верх' if i == 0 else 'Низ'}** | Открыто: **{revealed}/{safe_cells}**{timer_text}",
                        view=new_view
                    )
                except:
                    pass
        except:
            pass
    
    async def handle_game_over(self, interaction: discord.Interaction, view: MinesweeperView):
        game = view.game
        thread = interaction.channel
        
        # Показываем все бомбы
        for x, y in game.mines:
            game.cells_revealed.add((x, y))
        
        # Определяем в каком блоке проиграли
        failed_block_idx = 0 if self.y < 5 else 1
        
        # Удаляем ВСЕ сообщения кроме того где проиграли
        for i, msg_id in enumerate(game.message_ids):
            try:
                msg = await thread.fetch_message(msg_id)
                if i == failed_block_idx:
                    # Обновляем блок где проиграли
                    failed_view = MinesweeperView(game, view.user_id, view.thread_id, i)
                    for item in failed_view.children:
                        item.disabled = True
                    await msg.edit(
                        content=f"💀 **ВЫ ПРОИГРАЛИ НА БЛОКЕ {game.blocks_cleared + 1}**",
                        view=failed_view
                    )
                else:
                    # Удаляем другой блок
                    await msg.delete()
            except:
                pass
        
        # Сохраняем статистику
        total_time = time.time() - game.start_time
        avg_speed = game.blocks_cleared / total_time if total_time > 0 and game.blocks_cleared > 0 else 0
        
        async with bot.db_pool.acquire() as conn:
            if game.mode == 'hardcore':
                await conn.execute('''
                    INSERT INTO players (user_id, username, total_blocks_cleared, total_time_spent, best_speed, games_played, best_blocks_hardcore)
                    VALUES ($1, $2, $3, $4, $5, 1, $6)
                    ON CONFLICT (user_id) DO UPDATE SET
                        total_blocks_cleared = players.total_blocks_cleared + $3,
                        total_time_spent = players.total_time_spent + $4,
                        best_speed = CASE WHEN $5 > players.best_speed THEN $5 ELSE players.best_speed END,
                        games_played = players.games_played + 1,
                        best_blocks_hardcore = CASE WHEN $6 > players.best_blocks_hardcore THEN $6 ELSE players.best_blocks_hardcore END
                ''', interaction.user.id, str(interaction.user), game.blocks_cleared, total_time, avg_speed, game.blocks_cleared)
            else:
                await conn.execute('''
                    INSERT INTO players (user_id, username, total_blocks_cleared, total_time_spent, best_speed, games_played, best_blocks_normal)
                    VALUES ($1, $2, $3, $4, $5, 1, $6)
                    ON CONFLICT (user_id) DO UPDATE SET
                        total_blocks_cleared = players.total_blocks_cleared + $3,
                        total_time_spent = players.total_time_spent + $4,
                        best_speed = CASE WHEN $5 > players.best_speed THEN $5 ELSE players.best_speed END,
                        games_played = players.games_played + 1,
                        best_blocks_normal = CASE WHEN $6 > players.best_blocks_normal THEN $6 ELSE players.best_blocks_normal END
                ''', interaction.user.id, str(interaction.user), game.blocks_cleared, total_time, avg_speed, game.blocks_cleared)
            
            total_blocks = await conn.fetchval('SELECT total_blocks_cleared FROM players WHERE user_id = $1', interaction.user.id)
            total_time_all = await conn.fetchval('SELECT total_time_spent FROM players WHERE user_id = $1', interaction.user.id)
            new_avg_speed = total_blocks / total_time_all if total_time_all > 0 else 0
            
            await conn.execute('''
                INSERT INTO speed_leaderboard (user_id, username, avg_speed, total_blocks, total_time)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    avg_speed = $3, total_blocks = $4, total_time = $5, last_updated = NOW()
            ''', interaction.user.id, str(interaction.user), new_avg_speed, total_blocks, total_time_all)
            
            await conn.execute('DELETE FROM active_games WHERE thread_id = $1', view.thread_id)
        
        # Отправляем статистику
        mode_emoji = "💀" if game.mode == "hardcore" else "💣"
        stats_msg = (
            f"{mode_emoji} **ИГРА ОКОНЧЕНА!**\n\n"
            f"📊 **Статистика:**\n"
            f"├ Блоков пройдено: **{game.blocks_cleared}**\n"
            f"├ Время игры: **{total_time:.1f}с**\n"
            f"├ Лучшая скорость: **{avg_speed:.3f}** блоков/сек\n"
            f"└ Средняя скорость: **{new_avg_speed:.3f}** блоков/сек (всего)"
        )
        
        await thread.send(stats_msg)
    
    async def handle_field_complete(self, interaction: discord.Interaction, view: MinesweeperView):
        game = view.game
        thread = interaction.channel
        game.blocks_cleared += 1
        
        if game.mode == 'hardcore':
            bonus = game.get_time_bonus_hardcore()
            game.hardcore_timer += bonus
        
        # Удаляем старые сообщения
        for msg_id in game.message_ids:
            try:
                msg = await thread.fetch_message(msg_id)
                await msg.delete()
            except:
                pass
        game.message_ids.clear()
        
        # Генерируем новое поле
        game.generate_field(game.blocks_cleared)
        game.cells_revealed.clear()
        game.first_click = True
        
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE active_games 
                SET blocks_cleared = $1, last_action_time = NOW(), hardcore_timer = $2
                WHERE thread_id = $3
            ''', game.blocks_cleared, game.hardcore_timer, view.thread_id)
        
        await send_game_blocks(thread, game, view.user_id, view.thread_id)

async def send_game_blocks(thread, game: MinesweeperGame, user_id: int, thread_id: int):
    """Отправляет два блока 5x5"""
    timer_text = ""
    if game.mode == 'hardcore':
        timer_text = f" | ⏱️ **{game.hardcore_timer:.1f}с**"
    
    safe_cells = len([1 for y in range(10) for x in range(5) if game.grid[y][x] != -1])
    
    # Верхний блок
    view1 = MinesweeperView(game, user_id, thread_id, 0)
    msg1 = await thread.send(
        f"🎮 **Блок {game.blocks_cleared + 1} - Верх** | Открыто: **0/{safe_cells}**{timer_text}",
        view=view1
    )
    game.message_ids.append(msg1.id)
    
    # Нижний блок
    view2 = MinesweeperView(game, user_id, thread_id, 1)
    msg2 = await thread.send(
        f"🎮 **Блок {game.blocks_cleared + 1} - Низ** | Открыто: **0/{safe_cells}**{timer_text}",
        view=view2
    )
    game.message_ids.append(msg2.id)

@bot.tree.command(name="minesweeper", description="Начать игру в бесконечный сапёр")
@app_commands.describe(
    mode="Режим игры",
    multiplayer="Игра для всех в канале"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Обычный", value="normal"),
    app_commands.Choice(name="Хардкор (с таймером)", value="hardcore")
])
async def minesweeper(interaction: discord.Interaction, mode: str = "normal", multiplayer: bool = False):
    try:
        await interaction.response.defer(ephemeral=True)
    except:
        return
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO players (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        ''', interaction.user.id, str(interaction.user))
    
    mode_name = "💀 Хардкор" if mode == "hardcore" else "🎮 Обычный"
    mp_text = "👥 Мультиплеер" if multiplayer else f"👤 {interaction.user.display_name}"
    thread = await interaction.channel.create_thread(
        name=f"Сапёр: {mode_name} | {mp_text}",
        auto_archive_duration=60
    )
    
    game = MinesweeperGame(mode=mode, is_multiplayer=multiplayer)
    game.generate_field(0)
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO active_games (thread_id, user_id, mode, start_time, last_action_time, is_multiplayer, hardcore_timer)
            VALUES ($1, $2, $3, NOW(), NOW(), $4, $5)
        ''', thread.id, interaction.user.id, mode, multiplayer, game.hardcore_timer)
    
    welcome_text = f"🎮 **Бесконечный Сапёр - {mode_name}**\n\n"
    if mode == "hardcore":
        welcome_text += "⏱️ Ограниченное время! За блоки дается бонус\n"
        welcome_text += "⚡ Сложность растёт с каждым блоком!\n\n"
    else:
        welcome_text += "✨ Открывайте безопасные клетки\n"
        welcome_text += "🎯 Первый клик всегда безопасный!\n\n"
    
    if multiplayer:
        welcome_text += "👥 Любой может участвовать!\n"
    
    welcome_text += "Удачи! 🍀"
    
    await thread.send(welcome_text)
    await send_game_blocks(thread, game, interaction.user.id, thread.id)
    
    if mode == "hardcore":
        bot.loop.create_task(hardcore_timer_loop(thread.id, game))
    
    await interaction.followup.send(f"✅ Игра создана! {thread.mention}", ephemeral=True)

async def hardcore_timer_loop(thread_id: int, game: MinesweeperGame):
    while game.hardcore_timer > 0:
        await asyncio.sleep(0.5)
        game.hardcore_timer -= 0.5
        
        if game.hardcore_timer <= 0:
            try:
                thread = bot.get_channel(thread_id)
                if thread:
                    async with bot.db_pool.acquire() as conn:
                        user_id = await conn.fetchval('SELECT user_id FROM active_games WHERE thread_id = $1', thread_id)
                        
                        total_time = time.time() - game.start_time
                        avg_speed = game.blocks_cleared / total_time if total_time > 0 and game.blocks_cleared > 0 else 0
                        
                        if user_id:
                            await conn.execute('''
                                INSERT INTO players (user_id, username, total_blocks_cleared, total_time_spent, best_speed, games_played, best_blocks_hardcore)
                                VALUES ($1, '', $2, $3, $4, 1, $5)
                                ON CONFLICT (user_id) DO UPDATE SET
                                    total_blocks_cleared = players.total_blocks_cleared + $2,
                                    total_time_spent = players.total_time_spent + $3,
                                    best_speed = CASE WHEN $4 > players.best_speed THEN $4 ELSE players.best_speed END,
                                    games_played = players.games_played + 1,
                                    best_blocks_hardcore = CASE WHEN $5 > players.best_blocks_hardcore THEN $5 ELSE players.best_blocks_hardcore END
                            ''', user_id, game.blocks_cleared, total_time, avg_speed, game.blocks_cleared)
                        
                        await conn.execute('DELETE FROM active_games WHERE thread_id = $1', thread_id)
                    
                    await thread.send(f"⏰ **ВРЕМЯ ВЫШЛО!** Блоков: {game.blocks_cleared} | Скорость: {avg_speed:.3f}/с")
            except:
                pass
            break

@bot.tree.command(name="leaderboard", description="Таблица лидеров")
async def leaderboard(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except:
        return
    
    async with bot.db_pool.acquire() as conn:
        records = await conn.fetch('''
            SELECT username, best_speed, total_blocks_cleared, games_played
            FROM players WHERE best_speed > 0 ORDER BY best_speed DESC LIMIT 10
        ''')
        
        if not records:
            await interaction.followup.send("🏆 Таблица лидеров пуста!")
            return
        
        embed = discord.Embed(
            title="🏆 Таблица Лидеров",
            description="**Лучшая Скорость** - лучший результат за игру",
            color=discord.Color.gold()
        )
        
        medals = ["🥇", "🥈", "🥉"]
        text = ""
        
        for i, r in enumerate(records):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            text += f"{medal} **{r['username']}**\n"
            text += f"    ⚡ **{r['best_speed']:.3f}** блоков/сек\n"
            text += f"    📊 Игр: {r['games_played']} | Блоков: {r['total_blocks_cleared']}\n\n"
        
        embed.description += f"\n\n{text}"
    
    view = LeaderboardView("best")
    await interaction.followup.send(embed=embed, view=view)

class LeaderboardView(discord.ui.View):
    def __init__(self, current_type: str):
        super().__init__(timeout=120)
        self.current_type = current_type
        self.update_button()
    
    def update_button(self):
        self.clear_items()
        
        if self.current_type == "best":
            button = discord.ui.Button(label="⚡ Средняя скорость", style=discord.ButtonStyle.primary)
            button.callback = self.show_average
        else:
            button = discord.ui.Button(label="🏆 Лучшая скорость", style=discord.ButtonStyle.success)
            button.callback = self.show_best
        
        self.add_item(button)
    
    async def show_average(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except:
            return
        
        async with bot.db_pool.acquire() as conn:
            records = await conn.fetch('''
                SELECT username, avg_speed, total_blocks, total_time
                FROM speed_leaderboard WHERE avg_speed > 0 ORDER BY avg_speed DESC LIMIT 10
            ''')
            
            embed = discord.Embed(
                title="🏆 Таблица Лидеров",
                description="**Средняя Скорость** - общий коэффициент",
                color=discord.Color.blue()
            )
            
            medals = ["🥇", "🥈", "🥉"]
            text = ""
            
            for i, r in enumerate(records):
                medal = medals[i] if i < 3 else f"`{i+1}.`"
                text += f"{medal} **{r['username']}**\n"
                text += f"    ⚡ **{r['avg_speed']:.3f}** блоков/сек\n"
                text += f"    📊 {r['total_blocks']} блоков за {int(r['total_time']//60)}м\n\n"
            
            embed.description += f"\n\n{text}"
        
        self.current_type = "average"
        self.update_button()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def show_best(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except:
            return
        
        async with bot.db_pool.acquire() as conn:
            records = await conn.fetch('''
                SELECT username, best_speed, total_blocks_cleared, games_played
                FROM players WHERE best_speed > 0 ORDER BY best_speed DESC LIMIT 10
            ''')
            
            embed = discord.Embed(
                title="🏆 Таблица Лидеров",
                description="**Лучшая Скорость** - лучший результат за игру",
                color=discord.Color.gold()
            )
            
            medals = ["🥇", "🥈", "🥉"]
            text = ""
            
            for i, r in enumerate(records):
                medal = medals[i] if i < 3 else f"`{i+1}.`"
                text += f"{medal} **{r['username']}**\n"
                text += f"    ⚡ **{r['best_speed']:.3f}** блоков/сек\n"
                text += f"    📊 Игр: {r['games_played']} | Блоков: {r['total_blocks_cleared']}\n\n"
            
            embed.description += f"\n\n{text}"
        
        self.current_type = "best"
        self.update_button()
        await interaction.edit_original_response(embed=embed, view=self)

@bot.tree.command(name="profile", description="Профиль игрока")
@app_commands.describe(user="Пользователь")
async def profile(interaction: discord.Interaction, user: discord.User = None):
    try:
        await interaction.response.defer()
    except:
        return
    
    target = user or interaction.user
    
    async with bot.db_pool.acquire() as conn:
        player = await conn.fetchrow('SELECT * FROM players WHERE user_id = $1', target.id)
        
        if not player:
            await interaction.followup.send(f"❌ Профиль не найден!")
            return
        
        avg_data = await conn.fetchrow('SELECT avg_speed FROM speed_leaderboard WHERE user_id = $1', target.id)
        best_rank = await conn.fetchval('SELECT COUNT(*) + 1 FROM players WHERE best_speed > $1', player['best_speed'])
        
        avg_rank = None
        if avg_data:
            avg_rank = await conn.fetchval('SELECT COUNT(*) + 1 FROM speed_leaderboard WHERE avg_speed > $1', avg_data['avg_speed'])
    
    embed = discord.Embed(title=f"📊 Профиль", description=f"**{target.display_name}**", color=discord.Color.blue())
    embed.set_thumbnail(url=target.display_avatar.url)
    
    stats = f"""╔══════════════════════════════╗
║     СТАТИСТИКА               ║
╠══════════════════════════════╣
║ Игр:   {player['games_played']:>22} ║
║ Блоков: {player['total_blocks_cleared']:>21} ║
║ Время:  {f"{int(player['total_time_spent']//60)}м":>21} ║
╚══════════════════════════════╝"""
    
    embed.add_field(name="📈 Общая статистика", value=f"```\n{stats}\n```", inline=False)
    
    records = f"🏆 **Лучшая:** {player['best_speed']:.3f} блоков/с (#{best_rank})\n"
    if avg_data:
        records += f"⚡ **Средняя:** {avg_data['avg_speed']:.3f} блоков/с (#{avg_rank})\n"
    records += f"🎮 **Обычный:** {player['best_blocks_normal']} блоков\n"
    records += f"💀 **Хардкор:** {player['best_blocks_hardcore']} блоков"
    
    embed.add_field(name="🏅 Рекорды", value=records, inline=False)
    
    await interaction.followup.send(embed=embed)

async def health_check(request):
    return web.Response(text='OK', status=200)

async def start_http_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f'🌐 HTTP server на порту {PORT}')

@bot.event
async def on_ready():
    print(f'✅ Бот: {bot.user}')
    print(f'📊 Серверов: {len(bot.guilds)}')

@bot.event
async def on_thread_delete(thread):
    async with bot.db_pool.acquire() as conn:
        await conn.execute('DELETE FROM active_games WHERE thread_id = $1', thread.id)

async def main():
    await start_http_server()
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
