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
DATABASE_URL = os.getenv('DATABASE_URL')  # Session pooler connection string
TOKEN = os.getenv('DISCORD_TOKEN')
PORT = int(os.getenv('PORT', 10000))  # Render использует переменную PORT

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
            # Таблица игроков
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
            
            # Таблица активных игр
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
            
            # Таблица для средней скорости (отдельный лидерборд)
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
            
            # Индексы для производительности
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_players_best_speed ON players(best_speed DESC)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_leaderboard ON speed_leaderboard(avg_speed DESC)')

bot = MinesweeperBot()

class MinesweeperGame:
    def __init__(self, mode='normal', is_multiplayer=False):
        self.mode = mode  # 'normal' или 'hardcore'
        self.is_multiplayer = is_multiplayer
        self.blocks_cleared = 0
        self.current_block_index = 0
        self.start_time = time.time()
        self.last_action_time = time.time()
        self.hardcore_timer = 30.0 if mode == 'hardcore' else 0
        self.current_blocks = []
        self.cells_revealed = set()
        self.message_ids = []
        
    def generate_blocks(self, difficulty_level=0):
        """Генерирует два связанных блока 5x5"""
        blocks = []
        
        # В обычном режиме - фиксированное количество бомб
        # В хардкоре - прогрессия сложности
        if self.mode == 'hardcore':
            base_mines = 5
            mines_per_block = min(base_mines + (difficulty_level // 3), 12)
        else:
            mines_per_block = 5  # Фиксированная сложность
        
        for block_num in range(2):
            grid = [[0 for _ in range(5)] for _ in range(5)]
            mines = set()
            
            # Размещаем бомбы
            while len(mines) < mines_per_block:
                x, y = random.randint(0, 4), random.randint(0, 4)
                if (x, y) not in mines:
                    mines.add((x, y))
                    grid[y][x] = -1
            
            # Вычисляем числа
            for y in range(5):
                for x in range(5):
                    if grid[y][x] != -1:
                        count = 0
                        for dy in [-1, 0, 1]:
                            for dx in [-1, 0, 1]:
                                ny, nx = y + dy, x + dx
                                if 0 <= ny < 5 and 0 <= nx < 5 and grid[ny][nx] == -1:
                                    count += 1
                        grid[y][x] = count
            
            blocks.append({'grid': grid, 'mines': mines})
        
        return blocks
    
    def get_time_bonus_hardcore(self):
        """Вычисляет бонус времени за пройденный блок в хардкоре"""
        # Постепенно уменьшающийся бонус времени
        base_bonus = 18
        # Каждые 5 блоков уменьшаем бонус на 1 секунду
        reduction = (self.blocks_cleared // 5) * 1
        bonus = max(5, base_bonus - reduction)
        return bonus
    
    def get_initial_time_hardcore(self):
        """Начальное время для нового блока в хардкоре"""
        # Постепенно уменьшающееся начальное время
        base_time = 30
        # Каждые 3 блока уменьшаем на 1 секунду
        reduction = (self.blocks_cleared // 3) * 1
        return max(10, base_time - reduction)
    
    def reveal_cell(self, block_idx, x, y):
        """Открывает клетку и возвращает результат"""
        if block_idx >= len(self.current_blocks):
            return 'invalid', set()
        
        block = self.current_blocks[block_idx]
        grid = block['grid']
        
        if (block_idx, x, y) in self.cells_revealed:
            return 'already_revealed', set()
        
        if grid[y][x] == -1:
            return 'mine', {(block_idx, x, y)}
        
        # Flood fill для пустых клеток
        revealed = set()
        stack = [(x, y)]
        
        while stack:
            cx, cy = stack.pop()
            if (block_idx, cx, cy) in self.cells_revealed or (block_idx, cx, cy) in revealed:
                continue
            
            revealed.add((block_idx, cx, cy))
            
            if grid[cy][cx] == 0:
                for dy in [-1, 0, 1]:
                    for dx in [-1, 0, 1]:
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < 5 and 0 <= ny < 5:
                            if (block_idx, nx, ny) not in self.cells_revealed:
                                stack.append((nx, ny))
        
        return 'safe', revealed
    
    def is_block_complete(self):
        """Проверяет, пройдены ли оба блока"""
        total_safe_cells = 0
        revealed_safe_cells = 0
        
        for block_idx, block in enumerate(self.current_blocks):
            for y in range(5):
                for x in range(5):
                    if block['grid'][y][x] != -1:
                        total_safe_cells += 1
                        if (block_idx, x, y) in self.cells_revealed:
                            revealed_safe_cells += 1
        
        return revealed_safe_cells == total_safe_cells

class MinesweeperView(discord.ui.View):
    def __init__(self, game: MinesweeperGame, user_id: int, thread_id: int):
        super().__init__(timeout=None)
        self.game = game
        self.user_id = user_id
        self.thread_id = thread_id
        self.block_idx = 0
        self.update_buttons()
    
    def update_buttons(self):
        self.clear_items()
        
        if self.block_idx >= len(self.game.current_blocks):
            return
        
        block = self.game.current_blocks[self.block_idx]
        grid = block['grid']
        
        for y in range(5):
            for x in range(5):
                button = MinesweeperButton(x, y, self.block_idx, grid[y][x])
                
                if (self.block_idx, x, y) in self.game.cells_revealed:
                    button.disabled = True
                    value = grid[y][x]
                    if value == 0:
                        button.label = '·'
                        button.style = discord.ButtonStyle.secondary
                    else:
                        button.label = str(value)
                        button.style = discord.ButtonStyle.primary
                
                self.add_item(button)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.game.is_multiplayer and interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваша игра!", ephemeral=True)
            return False
        return True

class MinesweeperButton(discord.ui.Button):
    def __init__(self, x: int, y: int, block_idx: int, value: int):
        super().__init__(style=discord.ButtonStyle.secondary, label='⬜', row=y)
        self.x = x
        self.y = y
        self.block_idx = block_idx
        self.cell_value = value
    
    async def callback(self, interaction: discord.Interaction):
        view: MinesweeperView = self.view
        game = view.game
        
        # Обновляем время последнего действия
        current_time = time.time()
        game.last_action_time = current_time
        
        result, revealed = game.reveal_cell(self.block_idx, self.x, self.y)
        
        if result == 'invalid' or result == 'already_revealed':
            await interaction.response.defer()
            return
        
        if result == 'mine':
            # Игра окончена
            await self.handle_game_over(interaction, view)
            return
        
        # Добавляем открытые клетки
        game.cells_revealed.update(revealed)
        
        # Проверяем, пройдены ли оба блока
        if game.is_block_complete():
            await self.handle_block_complete(interaction, view)
        else:
            view.update_buttons()
            
            # Обновляем хардкор таймер
            timer_text = ""
            if game.mode == 'hardcore':
                timer_text = f"\n⏱️ Осталось времени: **{game.hardcore_timer:.1f}с**"
            
            await interaction.response.edit_message(
                content=f"🎮 Блок {game.blocks_cleared + 1} | Открыто клеток: {len(game.cells_revealed)}/{50 - len(game.current_blocks[0]['mines']) - len(game.current_blocks[1]['mines'])}{timer_text}",
                view=view
            )
    
    async def handle_game_over(self, interaction: discord.Interaction, view: MinesweeperView):
        game = view.game
        
        # Показываем все бомбы
        for block_idx, block in enumerate(game.current_blocks):
            for y in range(5):
                for x in range(5):
                    if block['grid'][y][x] == -1:
                        game.cells_revealed.add((block_idx, x, y))
        
        view.update_buttons()
        for item in view.children:
            item.disabled = True
        
        # Сохраняем статистику
        total_time = time.time() - game.start_time
        avg_speed = game.blocks_cleared / total_time if total_time > 0 and game.blocks_cleared > 0 else 0
        
        async with bot.db_pool.acquire() as conn:
            # Обновляем игрока
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
            
            # Обновляем speed leaderboard
            total_blocks = await conn.fetchval(
                'SELECT total_blocks_cleared FROM players WHERE user_id = $1',
                interaction.user.id
            )
            total_time_all = await conn.fetchval(
                'SELECT total_time_spent FROM players WHERE user_id = $1',
                interaction.user.id
            )
            
            new_avg_speed = total_blocks / total_time_all if total_time_all > 0 else 0
            
            await conn.execute('''
                INSERT INTO speed_leaderboard (user_id, username, avg_speed, total_blocks, total_time)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    avg_speed = $3,
                    total_blocks = $4,
                    total_time = $5,
                    last_updated = NOW()
            ''', interaction.user.id, str(interaction.user), new_avg_speed, total_blocks, total_time_all)
            
            # Удаляем активную игру
            await conn.execute('DELETE FROM active_games WHERE thread_id = $1', view.thread_id)
        
        mode_emoji = "💀" if game.mode == "hardcore" else "💣"
        await interaction.response.edit_message(
            content=f"{mode_emoji} **ИГРА ОКОНЧЕНА!**\n"
                    f"Блоков пройдено: **{game.blocks_cleared}**\n"
                    f"Время игры: **{total_time:.2f}с**\n"
                    f"Средняя скорость: **{avg_speed:.3f} блоков/сек**",
            view=view
        )
    
    async def handle_block_complete(self, interaction: discord.Interaction, view: MinesweeperView):
        game = view.game
        game.blocks_cleared += 1
        
        # Обновляем хардкор таймер с постепенным уменьшением
        if game.mode == 'hardcore':
            bonus = game.get_time_bonus_hardcore()
            game.hardcore_timer += bonus
        
        # Удаляем старые сообщения
        thread = interaction.channel
        try:
            for msg_id in view.game.message_ids:
                try:
                    msg = await thread.fetch_message(msg_id)
                    await msg.delete()
                except:
                    pass
            view.game.message_ids.clear()
        except:
            pass
        
        # Генерируем новые блоки
        game.current_blocks = game.generate_blocks(game.blocks_cleared)
        game.cells_revealed.clear()
        
        # Сохраняем прогресс
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE active_games 
                SET blocks_cleared = $1, last_action_time = NOW(), hardcore_timer = $2
                WHERE thread_id = $3
            ''', game.blocks_cleared, game.hardcore_timer, view.thread_id)
        
        await interaction.response.defer()
        
        # Отправляем новые блоки
        await send_game_blocks(thread, game, view.user_id, view.thread_id)

async def send_game_blocks(thread, game: MinesweeperGame, user_id: int, thread_id: int):
    """Отправляет два блока 5x5 в тред"""
    
    timer_text = ""
    if game.mode == 'hardcore':
        timer_text = f"\n⏱️ Осталось времени: **{game.hardcore_timer:.1f}с**"
    
    # Блок 1
    view1 = MinesweeperView(game, user_id, thread_id)
    view1.block_idx = 0
    msg1 = await thread.send(
        f"🎮 **Блок {game.blocks_cleared + 1} - Часть 1/2**{timer_text}",
        view=view1
    )
    game.message_ids.append(msg1.id)
    
    # Блок 2
    view2 = MinesweeperView(game, user_id, thread_id)
    view2.block_idx = 1
    msg2 = await thread.send(
        f"🎮 **Блок {game.blocks_cleared + 1} - Часть 2/2**{timer_text}",
        view=view2
    )
    game.message_ids.append(msg2.id)

@bot.tree.command(name="minesweeper", description="Начать игру в бесконечный сапёр")
@app_commands.describe(
    mode="Режим игры: normal или hardcore",
    multiplayer="Игра для всех в канале (по умолчанию - только для вас)"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Обычный", value="normal"),
    app_commands.Choice(name="Хардкор (с таймером)", value="hardcore")
])
async def minesweeper(interaction: discord.Interaction, mode: str = "normal", multiplayer: bool = False):
    await interaction.response.defer()
    
    # Создаем игрока если не существует
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO players (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        ''', interaction.user.id, str(interaction.user))
    
    # Создаем тред
    mode_name = "💀 Хардкор" if mode == "hardcore" else "🎮 Обычный"
    mp_text = "👥 Мультиплеер" if multiplayer else f"👤 {interaction.user.display_name}"
    thread = await interaction.channel.create_thread(
        name=f"Сапёр: {mode_name} | {mp_text}",
        auto_archive_duration=60
    )
    
    # Создаем игру
    game = MinesweeperGame(mode=mode, is_multiplayer=multiplayer)
    game.current_blocks = game.generate_blocks(0)
    
    # Сохраняем в БД
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO active_games (thread_id, user_id, mode, start_time, last_action_time, is_multiplayer, hardcore_timer)
            VALUES ($1, $2, $3, NOW(), NOW(), $4, $5)
        ''', thread.id, interaction.user.id, mode, multiplayer, game.hardcore_timer)
    
    # Отправляем приветствие
    welcome_text = f"🎮 **Бесконечный Сапёр - {mode_name}**\n\n"
    if mode == "hardcore":
        welcome_text += "⏱️ У вас есть ограниченное время на прохождение каждого блока!\n"
        welcome_text += "✅ За каждый пройденный блок вы получаете бонусное время\n"
        welcome_text += "⚡ С каждым блоком сложность растёт - больше бомб и меньше времени!\n\n"
    else:
        welcome_text += "✨ Открывайте все безопасные клетки, чтобы пройти блок\n"
        welcome_text += "🎯 Количество бомб фиксированное - играйте спокойно!\n\n"
    
    if multiplayer:
        welcome_text += "👥 Любой может нажимать на кнопки!\n"
    
    welcome_text += "Удачи! 🍀"
    
    await thread.send(welcome_text)
    
    # Отправляем блоки
    await send_game_blocks(thread, game, interaction.user.id, thread.id)
    
    # Запускаем таймер хардкора
    if mode == "hardcore":
        bot.loop.create_task(hardcore_timer_loop(thread.id, game))
    
    await interaction.followup.send(f"✅ Игра создана! {thread.mention}")

async def hardcore_timer_loop(thread_id: int, game: MinesweeperGame):
    """Цикл таймера для хардкор режима"""
    while game.hardcore_timer > 0:
        await asyncio.sleep(0.5)
        
        elapsed = time.time() - game.last_action_time
        game.hardcore_timer -= 0.5
        
        if game.hardcore_timer <= 0:
            # Время вышло!
            try:
                thread = bot.get_channel(thread_id)
                if thread:
                    # Завершаем игру
                    async with bot.db_pool.acquire() as conn:
                        user_id = await conn.fetchval(
                            'SELECT user_id FROM active_games WHERE thread_id = $1',
                            thread_id
                        )
                        
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
                            
                            # Обновляем speed leaderboard
                            total_blocks = await conn.fetchval(
                                'SELECT total_blocks_cleared FROM players WHERE user_id = $1',
                                user_id
                            )
                            total_time_all = await conn.fetchval(
                                'SELECT total_time_spent FROM players WHERE user_id = $1',
                                user_id
                            )
                            
                            new_avg_speed = total_blocks / total_time_all if total_time_all > 0 else 0
                            
                            await conn.execute('''
                                INSERT INTO speed_leaderboard (user_id, username, avg_speed, total_blocks, total_time)
                                VALUES ($1, '', $2, $3, $4)
                                ON CONFLICT (user_id) DO UPDATE SET
                                    avg_speed = $2,
                                    total_blocks = $3,
                                    total_time = $4,
                                    last_updated = NOW()
                            ''', user_id, new_avg_speed, total_blocks, total_time_all)
                        
                        await conn.execute('DELETE FROM active_games WHERE thread_id = $1', thread_id)
                    
                    await thread.send(
                        f"⏰ **ВРЕМЯ ВЫШЛО!**\n"
                        f"Блоков пройдено: **{game.blocks_cleared}**\n"
                        f"Время игры: **{total_time:.2f}с**\n"
                        f"Средняя скорость: **{avg_speed:.3f} блоков/сек**"
                    )
            except:
                pass
            break

@bot.tree.command(name="leaderboard", description="Таблица лидеров сапёра")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    
    # По умолчанию показываем лучшую скорость
    async with bot.db_pool.acquire() as conn:
        records = await conn.fetch('''
            SELECT username, best_speed, total_blocks_cleared, games_played
            FROM players
            WHERE best_speed > 0
            ORDER BY best_speed DESC
            LIMIT 10
        ''')
        
        if not records:
            await interaction.followup.send("🏆 Таблица лидеров пуста!")
            return
        
        embed = discord.Embed(
            title="🏆 Таблица Лидеров",
            description="**Лучшая Скорость** - лучший результат за одну игру",
            color=discord.Color.gold()
        )
        
        medals = ["🥇", "🥈", "🥉"]
        leaderboard_text = ""
        
        for i, record in enumerate(records):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            leaderboard_text += f"{medal} **{record['username']}**\n"
            leaderboard_text += f"    ⚡ **{record['best_speed']:.3f}** блоков/сек\n"
            leaderboard_text += f"    📊 Игр: {record['games_played']} | Блоков: {record['total_blocks_cleared']}\n\n"
        
        embed.description += f"\n\n{leaderboard_text}"
    
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
            button = discord.ui.Button(
                label="⚡ Показать среднюю скорость",
                style=discord.ButtonStyle.primary,
                emoji="📊"
            )
            button.callback = self.show_average
        else:
            button = discord.ui.Button(
                label="🏆 Показать лучшую скорость",
                style=discord.ButtonStyle.success,
                emoji="🎯"
            )
            button.callback = self.show_best
        
        self.add_item(button)
    
    async def show_average(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with bot.db_pool.acquire() as conn:
            records = await conn.fetch('''
                SELECT username, avg_speed, total_blocks, total_time
                FROM speed_leaderboard
                WHERE avg_speed > 0
                ORDER BY avg_speed DESC
                LIMIT 10
            ''')
            
            if not records:
                await interaction.followup.send("⚡ Таблица средней скорости пуста!", ephemeral=True)
                return
            
            embed = discord.Embed(
                title="🏆 Таблица Лидеров",
                description="**Средняя Скорость** - общий коэффициент (блоки ÷ время)",
                color=discord.Color.blue()
            )
            
            medals = ["🥇", "🥈", "🥉"]
            leaderboard_text = ""
            
            for i, record in enumerate(records):
                medal = medals[i] if i < 3 else f"`{i+1}.`"
                hours = int(record['total_time'] // 3600)
                minutes = int((record['total_time'] % 3600) // 60)
                time_str = f"{hours}ч {minutes}м" if hours > 0 else f"{minutes}м"
                
                leaderboard_text += f"{medal} **{record['username']}**\n"
                leaderboard_text += f"    ⚡ **{record['avg_speed']:.3f}** блоков/сек\n"
                leaderboard_text += f"    📊 {record['total_blocks']} блоков за {time_str}\n\n"
            
            embed.description += f"\n\n{leaderboard_text}"
        
        self.current_type = "average"
        self.update_button()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def show_best(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with bot.db_pool.acquire() as conn:
            records = await conn.fetch('''
                SELECT username, best_speed, total_blocks_cleared, games_played
                FROM players
                WHERE best_speed > 0
                ORDER BY best_speed DESC
                LIMIT 10
            ''')
            
            if not records:
                await interaction.followup.send("🏆 Таблица лидеров пуста!", ephemeral=True)
                return
            
            embed = discord.Embed(
                title="🏆 Таблица Лидеров",
                description="**Лучшая Скорость** - лучший результат за одну игру",
                color=discord.Color.gold()
            )
            
            medals = ["🥇", "🥈", "🥉"]
            leaderboard_text = ""
            
            for i, record in enumerate(records):
                medal = medals[i] if i < 3 else f"`{i+1}.`"
                leaderboard_text += f"{medal} **{record['username']}**\n"
                leaderboard_text += f"    ⚡ **{record['best_speed']:.3f}** блоков/сек\n"
                leaderboard_text += f"    📊 Игр: {record['games_played']} | Блоков: {record['total_blocks_cleared']}\n\n"
            
            embed.description += f"\n\n{leaderboard_text}"
        
        self.current_type = "best"
        self.update_button()
        await interaction.edit_original_response(embed=embed, view=self)

@bot.tree.command(name="profile", description="Ваш профиль в сапёре")
@app_commands.describe(user="Пользователь (оставьте пустым для своего профиля)")
async def profile(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer()
    
    target_user = user or interaction.user
    
    async with bot.db_pool.acquire() as conn:
        player = await conn.fetchrow('''
            SELECT * FROM players WHERE user_id = $1
        ''', target_user.id)
        
        if not player:
            if target_user == interaction.user:
                await interaction.followup.send(
                    "❌ У вас еще нет профиля! Сыграйте первую игру командой `/minesweeper`"
                )
            else:
                await interaction.followup.send(
                    f"❌ У {target_user.mention} еще нет профиля!"
                )
            return
        
        # Получаем среднюю скорость
        avg_speed_data = await conn.fetchrow('''
            SELECT avg_speed, total_blocks, total_time 
            FROM speed_leaderboard 
            WHERE user_id = $1
        ''', target_user.id)
        
        # Получаем позицию в рейтинге лучшей скорости
        best_rank = await conn.fetchval('''
            SELECT COUNT(*) + 1 
            FROM players 
            WHERE best_speed > $1
        ''', player['best_speed'])
        
        # Получаем позицию в рейтинге средней скорости
        avg_rank = None
        if avg_speed_data:
            avg_rank = await conn.fetchval('''
                SELECT COUNT(*) + 1 
                FROM speed_leaderboard 
                WHERE avg_speed > $1
            ''', avg_speed_data['avg_speed'])
    
    # Создаем красивый embed
    embed = discord.Embed(
        title=f"📊 Профиль игрока",
        description=f"**{target_user.display_name}**",
        color=discord.Color.blue()
    )
    
    embed.set_thumbnail(url=target_user.display_avatar.url)
    
    # Общая статистика в блоке кода
    stats_block = f"""╔══════════════════════════════╗
║     ОБЩАЯ СТАТИСТИКА         ║
╠══════════════════════════════╣
║ Игр сыграно:     {player['games_played']:>12} ║
║ Блоков пройдено: {player['total_blocks_cleared']:>12} ║
║ Времени потрачено: {f"{int(player['total_time_spent']//60)}м {int(player['total_time_spent']%60)}с":>10} ║
╚══════════════════════════════╝"""
    
    embed.add_field(
        name="📈 Общая статистика",
        value=f"```\n{stats_block}\n```",
        inline=False
    )
    
    # Рекорды
    records_text = f"🏆 **Лучшая скорость:** {player['best_speed']:.3f} блоков/сек\n"
    records_text += f"    └─ Место в рейтинге: **#{best_rank}**\n\n"
    
    if avg_speed_data:
        records_text += f"⚡ **Средняя скорость:** {avg_speed_data['avg_speed']:.3f} блоков/сек\n"
        records_text += f"    └─ Место в рейтинге: **#{avg_rank}**\n\n"
    
    records_text += f"🎮 **Лучший забег (обычный):** {player['best_blocks_normal']} блоков\n"
    records_text += f"💀 **Лучший забег (хардкор):** {player['best_blocks_hardcore']} блоков"
    
    embed.add_field(
        name="🏅 Рекорды",
        value=records_text,
        inline=False
    )
    
    # В среднем за игру
    if player['games_played'] > 0:
        avg_blocks_per_game = player['total_blocks_cleared'] / player['games_played']
        avg_time_per_game = player['total_time_spent'] / player['games_played']
        
        avg_text = f"📦 **Блоков за игру:** {avg_blocks_per_game:.1f}\n"
        avg_text += f"⏱️ **Время на игру:** {avg_time_per_game:.1f}с"
        
        embed.add_field(
            name="📊 В среднем за игру",
            value=avg_text,
            inline=True
        )
    
    embed.set_footer(text=f"Игрок с {player['created_at'].strftime('%d.%m.%Y')}")
    
    await interaction.followup.send(embed=embed)

# HTTP server для Render health check
async def health_check(request):
    """Health check endpoint для Render"""
    return web.Response(text='OK', status=200)

async def start_http_server():
    """Запускает HTTP сервер для health check"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f'🌐 HTTP server запущен на порту {PORT}')

@bot.event
async def on_ready():
    print(f'✅ Бот запущен как {bot.user}')
    print(f'📊 Серверов: {len(bot.guilds)}')
    print(f'⚡ База данных подключена')

@bot.event
async def on_thread_delete(thread):
    """Очистка при удалении треда"""
    async with bot.db_pool.acquire() as conn:
        await conn.execute('DELETE FROM active_games WHERE thread_id = $1', thread.id)

async def main():
    """Главная функция для запуска бота и HTTP сервера"""
    # Сначала запускаем HTTP сервер
    await start_http_server()
    
    # Затем запускаем бота
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
