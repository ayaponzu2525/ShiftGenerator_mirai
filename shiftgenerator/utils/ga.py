from datetime import datetime, timedelta, time
from django.utils import timezone 
from datetime import timedelta
from ..models import ShiftPreference, Staff, DayOfWeek, ShiftHistory,ShiftPreference,Holiday
import random
import pandas as pd
import numpy as np
from django.forms.models import model_to_dict



# 時間を分単位に変換する関数
def time_to_minutes(time_str):
    if pd.isna(time_str):  # NaNチェック
        return np.nan
    if isinstance(time_str, time):  # datetime.timeの場合
        return time_str.hour * 60 + time_str.minute
    # 文字列の場合の処理
    hours, minutes = map(int, time_str.split(':'))
    return hours * 60 + minutes

def minutes_to_time(minutes):
    if pd.isna(minutes):  # NaNチェック
        return "00:00"
    hours = int(minutes // 60)
    minutes = int(minutes % 60)
    return f"{hours:02}:{minutes:02}"

def create_initial_org():
    """
    シフト希望を元に初期集団を作成
    working_shifts: シフト希望データ
    """
    yesterday = timezone.now().date() - timedelta(days=1)
    #today = timezone.now().date()-1
    start_date = yesterday
    end_date = yesterday

    # シフト希望をf取得（勤務希望と休み希望を分けて取得）
    working_shifts = ShiftPreference.objects.filter(
        date__range=(start_date, end_date),
        holiday__isnull=True
    ).select_related('staff', 'day_of_week')
    
     # デバッグ用：シフト希望の数を表示
    print(f"取得したシフト希望の数: {working_shifts.count()}")
    
    initial_population = []
    
    # 希望100%反映した個体を1つ生成
    full_pref_solution = [model_to_dict(shift) for shift in working_shifts]  # 希望を辞書形式でコピー
    print(f"希望100%反映個体: {full_pref_solution}")  # デバッグ用
    initial_population.append(full_pref_solution)

    # ランダムに希望を減らした個体を複数生成
    for i in range(3):
        new_solution = []
        for shift in working_shifts:
            # 60%でどちらか一方、40%で両方
            if random.random() < 0.6:  # 60%の確率
                apply_reduce = True
                apply_adjust = random.choice([False, False, True])  # 40%の確率で調整
            else:  # 40%の確率
                apply_reduce = True
                apply_adjust = True

            if apply_reduce:
                reduced_shift = reduce_shift_randomly_within_range(shift)
                if reduced_shift is not None:
                    shift = reduced_shift

            if apply_adjust:
                adjusted_shift = adjust_shift_time(shift)
                if adjusted_shift is not None:
                    shift = adjusted_shift

            if shift is not None:
                new_solution.append(shift)
        print(f"生成した個体{i + 1}: {new_solution}")  # デバッグ用
        initial_population.append(new_solution)

    return initial_population




def adjust_shift_time(shift):
    """
    シフトの開始時間または終了時間を部分的に調整する関数
    shift: シフトの情報（例: {'starttime': '10:00', 'endtime': '18:00', 'staff_id': 1}）
    """
    print(f"元のシフト: {shift}")  # デバッグ用

    # ランダムにシフトを減らすかそのままにするかを選択
    if random.choice([True, False]):  # Falseならそのまま
        print("シフト時間を変更しない")
        return shift

    start_minutes = time_to_minutes(shift.starttime)  # 'starttime' を属性として取得
    end_minutes = time_to_minutes(shift.endtime)

    if random.choice([True, False]):
        # 開始時間を60〜180分遅らせる
        new_start_minutes = start_minutes + random.randint(1, 3) * 60
        if new_start_minutes >= end_minutes:
            print("削除: 開始時間が終了時間を超えた")
            return None
        shift.starttime = minutes_to_time(new_start_minutes)
        print(f"新しい開始時間: {shift.starttime}")
    else:
        # 終了時間を60〜120分早める
        new_end_minutes = end_minutes - random.randint(1, 2) * 60
        if new_end_minutes <= start_minutes:
            print("削除: 終了時間が開始時間を下回った")
            return None
        shift.endtime = minutes_to_time(new_end_minutes)
        print(f"新しい終了時間: {shift.endtime}")

    print(f"変更後のシフト: {shift}")  # デバッグ用
    return shift




def reduce_shift_randomly_within_range(shift, reduction_percentage_range=(0, 30)): #0~30%でシフトを削除
    """
    シフト希望全体を指定された割合でランダムに削除する関数
    shift: シフト情報
    reduction_percentage_range: 削除する割合の範囲（%）
    """
    print(f"元のシフト: {shift}")
    
    # 削除する確率を指定された範囲内でランダムに決定
    reduction_percentage = random.randint(reduction_percentage_range[0], reduction_percentage_range[1])
    
    # 削除するかどうかを割合に基づいて判断
    if random.random() * 100 < reduction_percentage:  # reduction_percentage%の確率で削除
        print(f"{reduction_percentage}%の確率で削除: シフト全体を削除します")
        return None  # シフト全体を削除

    print(f"{reduction_percentage}%の確率で削除されません")
    return shift  # シフトをそのままにして返す





# test_shift = pd.DataFrame({
#     'starttime': ['10:00', '12:30','8:30','9:00', '17:00'],
#     'endtime': ['18:00', '20:00', '17:30', '17:00', '20:00'],
#     'staff_id': [1, 2, 3, 4, 5]
# })

# 各行に対してreduce_shiftを適用
# for index, row in test_shift.iterrows():
#     shift = {
#         'starttime': row['starttime'],
#         'endtime': row['endtime'],
#         'staff_id': row['staff_id']
#     }
#     reduced_shift = reduce_shift_randomly_within_range(shift)
#     print(f"変更後: {reduced_shift}")

#スコア計算
def fitness_function(solution):
    # 1. 希望の反映度の計算
    preference_score = calculate_preference_score(solution)

    # 2. 業務要件の達成度の計算
    business_requirements_score = calculate_business_requirements_score(solution)

    # 3. 公平性の評価の計算
    fairness_score = calculate_fairness_score(solution)

    # 4. スキルの評価の計算
    skill_score = calculate_skill_score(solution)

    # 総合評価
    total_fitness = (weight_preference * preference_score +
                     weight_business * business_requirements_score +
                     weight_fairness * fairness_score +
                     weight_skill * skill_score)

    return total_fitness
