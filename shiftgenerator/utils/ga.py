from datetime import datetime, timedelta, time
from django.utils.timezone import now
#from ..models import ShiftPreference, Staff, DayOfWeek, ShiftHistory,ShiftPreference,Holiday
import random
import pandas as pd
import numpy as np



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

def create_initial_org(solution):
    
    today = timezone.now().date()
    start_date = today
    end_date = today

    # シフト希望をf取得（勤務希望と休み希望を分けて取得）
    working_shifts = ShiftPreference.objects.filter(
        date__range=(start_date, end_date),
        holiday__isnull=True
    ).select_related('staff', 'day_of_week')
    
    """
    シフト希望を元に初期集団を作成
    working_shifts: シフト希望データ
    """
    initial_population = []
    
    # 希望100%反映した個体を1つ生成
    full_pref_solution = [shift.copy() for shift in working_shifts]  # 希望をそのままコピー
    print(f"希望100%反映個体: {full_pref_solution}")  # デバッグ用
    initial_population.append(full_pref_solution)

    # ランダムに希望を減らした個体を複数生成
    for i in range(9):  # 例えば9つの個体をランダムに生成
        new_solution = []
        for shift in working_shifts:
            reduced_shift = reduce_shift(shift)
            if reduced_shift is not None:
                new_solution.append(reduced_shift)
        
        print(f"生成した個体{i + 1}: {new_solution}")  # デバッグ用
        initial_population.append(new_solution)

    print(f"最終的な初期集団: {initial_population}")  # デバッグ用
    return initial_population



import random

def reduce_shift(shift):
    """
    シフトを部分的に減らす、または完全に削除する関数
    shift: シフトの情報（例: {'starttime': '10:00', 'endtime': '18:00', 'staff_id': 1}）
    """
    print(f"元のシフト: {shift}")  # デバッグ用
    
    # ランダムに選択する（Trueなら全体削除、Falseなら部分削減）
    if random.choice([True, False]):
        print("シフト全体を削除")
        return None  # シフト全体を削除
    else:
        # 部分削減
        start_minutes = time_to_minutes(shift['starttime'])
        end_minutes = time_to_minutes(shift['endtime'])
        
        if random.choice([True, False]):
            # 開始時間を30分〜60分遅らせる
            new_start_minutes = start_minutes + random.randint(30, 60) #ここ
            if new_start_minutes >= end_minutes:
                print("削除: 開始時間が終了時間を超えた")
                return None
            shift['starttime'] = minutes_to_time(new_start_minutes)
            print(f"新しい開始時間: {shift['starttime']}")
        else:
            # 終了時間を30分〜60分早める
            new_end_minutes = end_minutes - random.randint(1, 4) * 60 #ここ
            if new_end_minutes <= start_minutes:
                print("削除: 終了時間が開始時間を下回った")
                return None
            shift['endtime'] = minutes_to_time(new_end_minutes)
            print(f"新しい終了時間: {shift['endtime']}")
    
    print(f"変更後のシフト: {shift}")  # デバッグ用
    return shift


test_shift = pd.DataFrame({
    'starttime': ['10:00', '12:30'],
    'endtime': ['18:00', '20:00'],
    'staff_id': [1, 2]
})

# 各行に対してreduce_shiftを適用
for index, row in test_shift.iterrows():
    shift = {
        'starttime': row['starttime'],
        'endtime': row['endtime'],
        'staff_id': row['staff_id']
    }
    reduced_shift = reduce_shift(shift)
    print(f"減らされたシフト: {reduced_shift}")

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
