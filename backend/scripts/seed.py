"""种子数据：四个 House、管理员账号、演示用题库（样例题，均标注为演示/样例）。

运行：python scripts/seed.py
幂等：已存在的对象跳过。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password, new_id  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    House, Question, QuestionVersion, User,
    POOL_OFFICIAL, POOL_PRACTICE, ROLE_ADMIN, ROLE_ADVISOR, ROLE_STUDENT,
)
from app.db.session import get_engine, make_session_factory  # noqa: E402

# 四院名称：取自校方确认的**四象**（青龙 / 白虎 / 玄武 / 朱雀）。
# 注意 `key` 是**技术标识**，出现在数据库外键、快照字段、前端映射里，
# 一律保持 tiger/dragon/tortoise/phoenix 不变 —— 只有给人看的名字改。
# 第 4 院原名是占位的「凤凰 / Phoenix」，正式名是「朱雀 / Vermilion Bird」：
# 凤凰（Fenghuang）与朱雀（Vermilion Bird）在中文语境里不是同一种神兽，
# 四象里的南方神兽是朱雀。历史比赛的成绩快照仍保留当时的名字，这是有意的。
HOUSES = [
    {"key": "tiger", "name_cn": "白虎", "name_en": "White Tiger",
     "color_light": "#DCE0E3", "color_deep": "#8E9BA6"},
    {"key": "dragon", "name_cn": "青龙", "name_en": "Azure Dragon",
     "color_light": "#5FBF9E", "color_deep": "#2E7D64"},
    {"key": "tortoise", "name_cn": "玄武", "name_en": "Black Tortoise",
     "color_light": "#8FB4C9", "color_deep": "#46587E"},
    {"key": "phoenix", "name_cn": "朱雀", "name_en": "Vermilion Bird",
     "color_light": "#E08A5E", "color_deep": "#B4502F"},
]

# 官方池样题（[样例] 前缀 + 演示说明；均为客观单选题，答案已人工核对）
OFFICIAL = [
    ("科学常识", "世界上最深的海沟是哪一条？", ["马里亚纳海沟", "波多黎各海沟", "爪哇海沟", "汤加海沟"], 0,
     "马里亚纳海沟最深处约 10900 米。", 1),
    ("科学常识", "人体中数量最多的细胞是以下哪一种？", ["红细胞", "神经元", "皮肤细胞", "肌细胞"], 0,
     "红细胞约占人体细胞总数的 80% 以上。", 1),
    ("科学常识", "光的传播速度在真空中约为每秒多少公里？", ["3 万公里", "30 万公里", "300 万公里", "3000 公里"], 1,
     "真空中光速约 299792 km/s，约 30 万公里/秒。", 1),
    ("科学常识", "元素周期表中符号 Fe 代表哪种元素？", ["氟", "铁", "碘", "银"], 1, "Fe 来自拉丁文 Ferrum。", 1),
    ("科学常识", "地球自转一周大约需要多长时间？", ["12 小时", "24 小时", "一个月", "一年"], 1, "约 23 时 56 分。", 1),
    ("生活与安全", "在厨房炒菜时油锅起火，以下哪种做法是正确的？",
     ["立即盖上锅盖并关闭炉灶", "泼一大杯水进油锅", "端起油锅移到水槽冲水", "打开抽油烟机吹散油烟"], 0,
     "隔绝空气可灭火；水入油锅会爆溅。", 1),
    ("生活与安全", "室外遇到雷雨时，以下哪个地点相对最安全？",
     ["空旷的操场上", "茂密的大树下", "有避雷设施的建筑物内", "山顶凉亭里"], 2,
     "有避雷设施的建筑物内最安全。", 1),
    ("生活与安全", "发现有人触电倒地，第一步应该怎么做？",
     ["直接用手拉开他", "先断开电源或用干燥木棍挑开电线", "立即做人工呼吸", "泼水叫醒他"], 1,
     "先确保自身与伤者脱离电源。", 1),
    ("生活与安全", "家庭药箱中的碘伏的主要用途是？", ["退烧", "皮肤消毒", "止痛", "助消化"], 1, "碘伏用于皮肤黏膜消毒。", 2),
    ("生活与安全", "火灾报警电话是多少？", ["110", "119", "120", "122"], 1, "119 为火警电话。", 1),
    ("地理文化", "世界上最长的河流是哪一条？", ["长江", "尼罗河", "亚马逊河", "密西西比河"], 1,
     "尼罗河约 6650 公里（常用口径）。", 1),
    ("地理文化", "澳大利亚的首都是哪座城市？", ["悉尼", "墨尔本", "堪培拉", "布里斯班"], 2, "堪培拉为首都。", 1),
    ("地理文化", "世界上面积最大的海洋是？", ["大西洋", "印度洋", "太平洋", "北冰洋"], 2, "太平洋面积约 1.65 亿平方公里。", 1),
    ("地理文化", "长城东端的山海关被称为？", ["天下第一关", "天下第一雄关", "塞上江南", "九塞之首"], 0,
     "山海关称天下第一关；嘉峪关称天下第一雄关。", 2),
    ("地理文化", "世界上国土面积最大的国家是？", ["中国", "加拿大", "美国", "俄罗斯"], 3, "俄罗斯约 1709 万平方公里。", 1),
    ("语言趣味", "成语『画蛇添足』的意思是？",
     ["锦上添花", "多此一举反而坏事", "精益求精", "画技高超"], 1, "比喻做多余的事反而不妥。", 1),
    ("语言趣味", "英语单词 'brief' 的含义是？", ["简短的", "明亮的", "沉重的", "昂贵的"], 0, "brief = 简短的。", 1),
    ("语言趣味", "『四面楚歌』与下列哪位历史人物相关？", ["项羽", "刘邦", "韩信", "张良"], 0,
     "楚汉相争末期项羽被困垓下。", 2),
    ("语言趣味", "下列哪一个是量词的正确用法？", ["一匹马", "一条马", "一只马", "一头马"], 0,
     "马常用量词为『匹』（一匹马）。", 1),
    ("语言趣味", "英文 'Economics' 对应的学科是？", ["经济学", "物理学", "地理学", "天文学"], 0, "Economics = 经济学。", 1),
    ("逻辑推理", "1、1、2、3、5、8、下一项是多少？", ["11", "12", "13", "14"], 2, "斐波那契数列：5+8=13。", 1),
    ("逻辑推理", "所有的猫都怕水，小花是一只猫，所以？", ["小花怕水", "小花不怕水", "无法确定", "小花喜欢水"], 0,
     "三段论直接结论。", 1),
    ("逻辑推理", "一个正方体有几个顶点？", ["6", "8", "12", "4"], 1, "正方体 8 个顶点。", 1),
    ("逻辑推理", "甲比乙高，丙比甲高，则三人中最矮的是？", ["甲", "乙", "丙", "无法确定"], 1,
     "丙>甲>乙，最矮是乙。", 1),
    ("逻辑推理", "时钟 3 点整，时针与分针的夹角是多少度？", ["30 度", "60 度", "90 度", "120 度"], 2, "3×30=90 度。", 1),
    ("校园知识", "国际象棋棋盘中一共有多少个方格？", ["36", "49", "64", "81"], 2, "8×8=64。", 1),
    ("校园知识", "以下哪个单位用于度量力？", ["焦耳", "牛顿", "瓦特", "帕斯卡"], 1, "力的单位是牛顿。", 1),
    ("校园知识", "化学实验中酒精灯的正确熄灭方式是？", ["用嘴吹灭", "用灯帽盖灭", "用水浇灭", "任其燃尽"], 1,
     "灯帽盖灭，防止引燃与灯芯损坏。", 1),
    ("校园知识", "一位 metre（米）的定义在现行国际单位制中依据什么确定？",
     ["光在真空中一定时间内走过的距离", "一根标准米尺", "地球周长的一千万分之一", "某人的步长"], 0,
     "1983 年起用光速定义米。", 2),
    ("校园知识", "俯卧撑主要锻炼的肌肉群不包括以下哪一项？", ["胸大肌", "肱三头肌", "股四头肌（主要承重）", "核心肌群"], 2,
     "俯卧撑主要训练上肢推力肌群。", 2),
    ("科学常识", "植物通过什么作用将光能转化为化学能？", ["呼吸作用", "光合作用", "蒸腾作用", "吸收作用"], 1,
     "光合作用。", 1),
    ("科学常识", "声音在下列哪种介质中传播最快？", ["真空", "空气", "水", "钢铁"], 3,
     "固体中最快；真空中声音无法传播。", 2),
    ("地理文化", "日本的首都是？", ["大阪", "京都", "东京", "名古屋"], 2, "东京。", 1),
    ("地理文化", "『山重水复疑无路，柳暗花明又一村』的作者是？", ["李白", "杜甫", "陆游", "白居易"], 2,
     "出自陆游《游山西村》。", 2),
    ("生活与安全", "被烫伤后的正确第一处置是？",
     ["涂牙膏", "用流动冷水冲 15 分钟以上", "挑破水泡", "用冰块直接敷"], 1,
     "流动冷水冲淋降温。", 1),
    ("语言趣味", "'Sofa' 一词的中文含义是？", ["书架", "沙发", "水槽", "炉灶"], 1, "sofa = 沙发（外来词）。", 1),
    ("逻辑推理", "2、4、8、16、下一项是？", ["20", "24", "32", "64"], 2, "等比数列 ×2。", 1),
    ("科学常识", "人体最大的器官是？", ["肝脏", "大脑", "皮肤", "肺"], 2, "皮肤是人体最大的器官。", 1),
    ("地理文化", "世界上人口最多的大洲是？", ["非洲", "亚洲", "欧洲", "南美洲"], 1, "亚洲。", 1),
    ("生活与安全", "骑自行车下陡坡时最安全的做法是？",
     ["捏住前后刹车减速", "松开全部车把", "站立加速冲下去", "闭眼滑行"], 0, "控制车速，前后刹车并用。", 1),
    ("校园知识", "标准的篮球比赛每队上场几名球员？", ["4", "5", "6", "7"], 1, "每队 5 人上场。", 1),
]

PRACTICE = [
    ("科学常识", "月亮自身会发光吗？", ["会", "不会，反射太阳光", "只在满月时发光", "科学界尚无定论"], 1,
     "月球反射太阳光。"),
    ("科学常识", "水在标准大气压下的沸点是？", ["90℃", "100℃", "110℃", "120℃"], 1, "100℃。"),
    ("科学常识", "下列哪种动物是哺乳动物？", ["鲨鱼", "海豚", "章鱼", "鳄鱼"], 1, "海豚是哺乳动物。"),
    ("生活与安全", "过马路时应当？", ["看手机快走", "走斑马线并观察信号灯", "翻越护栏", "跟着人群闯红灯"], 1, "遵守交通信号。"),
    ("生活与安全", "久坐之后突然站起眼前发黑，最可能是因为？", ["体位性低血压", "近视加深", "感冒", "中暑"], 0, "体位性低血压。"),
    ("地理文化", "中国的陆地邻国数量是多少个？", ["12", "14", "16", "10"], 1, "14 个陆地邻国。"),
    ("地理文化", "『世界屋脊』指的是？", ["青藏高原", "帕米尔高原", "云贵高原", "黄土高原"], 0, "青藏高原。"),
    ("语言趣味", "'Coffee' 的中文是？", ["可乐", "咖啡", "可可", "卡片"], 1, "咖啡。"),
    ("语言趣味", "成语『守株待兔』讽刺的是？", ["勤奋努力", "心存侥幸不劳而获", "善于等待", "爱护动物"], 1, "心存侥幸。"),
    ("逻辑推理", "3、6、9、12、下一项？", ["14", "15", "16", "18"], 1, "等差 3。"),
    ("逻辑推理", "五个小朋友排成一排，小红左边有 2 人，右边有几人？", ["1", "2", "3", "4"], 1, "5-2-1=2。"),
    ("校园知识", "汉字『日』的标准笔顺共几笔？", ["3", "4", "5", "6"], 1, "竖、横折、横、横，共 4 笔。"),
    ("校园知识", "下列哪个是计算机的输入设备？", ["显示器", "键盘", "音箱", "打印机"], 1, "键盘是输入设备。"),
    ("科学常识", "彩虹一般出现在雨后天空中的什么方向（北半球下午）？", ["东边", "西边", "南边", "北边"], 0,
     "下午彩虹在东方。"),
    ("科学常识", "空气中含量最多的气体是？", ["氧气", "氮气", "二氧化碳", "稀有气体"], 1, "氮气约 78%。"),
    ("生活与安全", "电梯突然下坠时比较合理的自我保护是？",
     ["按下所有楼层键并靠墙屈膝", "大声尖叫乱跳", "扒门爬出", "躺平在地板"], 0, "按亮楼层+屈膝减震。"),
    ("地理文化", "世界上最小的国家常被认为是？", ["摩纳哥", "梵蒂冈", "瑙鲁", "圣马力诺"], 1, "梵蒂冈约 0.44 平方公里。"),
    ("语言趣味", "『亡羊补牢』的寓意是？", ["为时已晚", "及时补救还不晚", "丢三落四", "羊圈要结实"], 1, "犹未迟也。"),
    ("逻辑推理", "星期一的后天之后一天是星期几？", ["星期三", "星期四", "星期五", "星期六"], 1, "周一+3=周四。"),
    ("校园知识", "物理中电流的单位是？", ["伏特", "安培", "欧姆", "瓦特"], 1, "安培。"),
    ("科学常识", "人体正常体温（口腔）约为？", ["35℃", "37℃", "39℃", "41℃"], 1, "约 36.5–37.5℃。"),
    ("地理文化", "长江注入哪个海？", ["黄海", "东海", "南海", "渤海"], 1, "东海。"),
    ("语言趣味", "'Gym' 通常指什么场所？", ["图书馆", "健身房", "实验室", "食堂"], 1, "gymnasium = 健身房。"),
    ("逻辑推理", "比 0.5 大且比 0.6 小的数是？", ["0.45", "0.55", "0.65", "0.4"], 1, "0.55。"),
    ("校园知识", "compass 在数学课堂中最可能指？", ["指南针/圆规", "计算器", "直尺", "地球仪"], 0, "compass=圆规（或指南针）。"),
    ("科学常识", "铁生锈是下列哪种变化？", ["物理变化", "化学变化", "没有变化", "生物变化"], 1, "氧化属化学变化。"),
    ("生活与安全", "以下哪种食物组合最均衡？", ["米饭+青菜+鸡蛋", "薯片+可乐", "糖果+蛋糕", "纯肉+纯肉"], 0, "主食+蔬果+蛋白。"),
    ("地理文化", "『兵马俑』位于今天的哪个城市附近？", ["西安", "洛阳", "南京", "成都"], 0, "西安临潼。"),
    ("语言趣味", "'September' 是九月，其词根 sept- 原意是『七』，这是因为？",
     ["古罗马历法从三月开始", "拼写错误流传", "英语借自法语", "随机约定"], 0, "古罗马历以三月为一年之始。"),
    ("逻辑推理", "一把剪刀 20 元，一把尺子 5 元，买 2 剪 3 尺共多少元？", ["45", "50", "55", "60"], 2, "40+15=55。"),
]


async def seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory()
    s = get_settings()
    async with factory() as db:
        # Houses
        id_by_key = {}
        for h in HOUSES:
            exists = (await db.execute(select(House).where(House.key == h["key"]))).scalar_one_or_none()
            if exists is None:
                hid = new_id()
                db.add(House(id=hid, key=h["key"], name_cn=h["name_cn"], name_en=h["name_en"],
                             color_light=h["color_light"], color_deep=h["color_deep"]))
                id_by_key[h["key"]] = hid
            else:
                id_by_key[h["key"]] = exists.id
        await db.flush()

        # Admin
        admin = (await db.execute(select(User).where(User.username == s.seed_admin_username))).scalar_one_or_none()
        if admin is None:
            db.add(User(id=new_id(), username=s.seed_admin_username,
                        password_hash=hash_password(s.seed_admin_password), role=ROLE_ADMIN,
                        must_change_password=True))
            print(f"[seed] 管理员 {s.seed_admin_username} 创建，初始密码见 MGUS_SEED_ADMIN_PASSWORD，请立即修改")

        # 演示账号（样例，标注 demo 前缀）
        for i in range(1, 6):
            for h in HOUSES:
                uname = f"demo_{h['key']}_{i:02d}"
                exists = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
                if exists is None:
                    db.add(User(id=new_id(), username=uname,
                                password_hash=hash_password("demo-pass-2026"),
                                role=ROLE_STUDENT, house_id=id_by_key[h["key"]],
                                display_name=f"{h['name_cn']}学员{i:02d}"))
                elif exists.role == ROLE_STUDENT:
                    exists.role = ROLE_STUDENT
                    exists.house_id = id_by_key[h["key"]]
        for h in HOUSES:
            uname = f"demo_advisor_{h['key']}"
            exists = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
            if exists is None:
                db.add(User(id=new_id(), username=uname,
                            password_hash=hash_password("demo-pass-2026"),
                            role=ROLE_ADVISOR, house_id=id_by_key[h["key"]]))
        student = (await db.execute(select(User).where(User.username == "demo_student"))).scalar_one_or_none()
        if student is None:
            db.add(User(id=new_id(), username="demo_student",
                        password_hash=hash_password("demo-pass-2026"), role=ROLE_STUDENT))
        await db.flush()

        # 题库（样例题；导入后处于 draft，需要管理员审核通过才可进入正式抽题池）
        async def ensure_questions(items, pool, prefix):
            created = 0
            for idx, item in enumerate(items):
                subject = item[0]
                prompt = f"[样例] {item[1]}"
                texts, correct_idx = item[2], item[3]
                explanation = item[4] if len(item) > 4 else None
                difficulty = item[5] if len(item) > 5 else 1
                from app.modules.questions.router import normalize_prompt
                ph = normalize_prompt(prompt)
                dup = (await db.execute(select(Question).where(
                    Question.prompt_hash == ph, Question.pool == pool))).scalar_one_or_none()
                if dup is not None:
                    continue
                qid, vid = new_id(), new_id()
                ids = [f"o{i+1}" for i in range(len(texts))]
                q = Question(id=qid, subject=subject, difficulty=difficulty, language="zh",
                             pool=pool, status="draft", prompt_hash=ph, created_by=None)
                v = QuestionVersion(id=vid, question_id=qid, version_no=1, prompt=prompt,
                                    options_json=[{"option_id": ids[i], "text": t} for i, t in enumerate(texts)],
                                    correct_option_id=ids[correct_idx], explanation=explanation,
                                    source="样例题库（演示数据，需学校审核）", review_state="pending")
                db.add_all([q, v])
                q.current_version_id = v.id
                created += 1
            return created

        c1 = await ensure_questions(OFFICIAL, POOL_OFFICIAL, "official")
        c2 = await ensure_questions(PRACTICE, POOL_PRACTICE, "practice")
        await db.commit()
        print(f"[seed] 完成：houses=4 官方样题新增 {c1} 练习样题新增 {c2}")
        print("[seed] 注意：样题均为 draft 状态。正式比赛前需管理员审核（approve）通过。")


if __name__ == "__main__":
    asyncio.run(seed())
