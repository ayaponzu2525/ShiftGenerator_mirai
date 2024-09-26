from django.http import HttpResponse, JsonResponse, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
import pandas as pd
import pickle
import os
from django.conf import settings
import numpy as np
import logging
from datetime import datetime, timedelta

import pytz
from django.contrib.auth import login
from .forms import CustomUserCreationForm, ShiftPreferenceForm
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login as auth_login, authenticate
from django.contrib import messages 
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from .models import ShiftPreference, Staff, DayOfWeek, ShiftHistory,ShiftPreference,Holiday
import traceback
from .forms import ShiftPreferenceForm
import json
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth.decorators import user_passes_test




def index(request):
    return render(request, 'shiftgenerator/home.html')


def save_shifts(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        changes = data.get('changes', [])

        # デバッグのために `changes` を出力
        print("Received changes:", changes)

        # `changes`をリストとしてループ処理
        for change in changes:
            action = change['action']
            item = change['item']

            # 開始時間と終了時間をISO形式からPythonのdatetimeに変換（UTCからローカルタイムに変換）
            start_datetime = timezone.make_aware(datetime.fromisoformat(item['start'].replace("Z", "")), pytz.UTC).astimezone(timezone.get_current_timezone())
            end_datetime = timezone.make_aware(datetime.fromisoformat(item['end'].replace("Z", "")), pytz.UTC).astimezone(timezone.get_current_timezone())

            # 日付部分と時間部分に分ける
            shift_date = start_datetime.date()  # 日付部分（YYYY-MM-DD）
            start_time = start_datetime.time()  # 開始時間部分（HH:MM:SS）
            end_time = end_datetime.time()      # 終了時間部分（HH:MM:SS）

            if action == 'add':
                # 日付から曜日を取得
                day_number = shift_date.weekday()  # 0: 月曜日, 6: 日曜日

                # DayOfWeek インスタンスを取得
                day_of_week_instance = DayOfWeek.objects.get(day_number=day_number)

                # 新しいシフトをデータベースに追加
                ShiftPreference.objects.create(
                    staff_id=item['group'],
                    date=shift_date,  # 日付を保存
                    confirmed_starttime=start_time,  # 開始時間を保存
                    confirmed_endtime=end_time,       # 終了時間を保存
                    day_of_week=day_of_week_instance   # 曜日を保存
                )
            elif action == 'update' or action == 'move':
                # 既存シフトを更新
                shift = ShiftPreference.objects.get(id=item['id'])
                shift.date = shift_date  # 日付を更新
                shift.confirmed_starttime = start_time  # 開始時間を更新
                shift.confirmed_endtime = end_time      # 終了時間を更新
                shift.staff_id = item['group']  # グループを更新
                shift.save()
            elif action == 'remove':
                # シフトの confirmed_starttime と confirmed_endtime を None に更新
                shift = ShiftPreference.objects.get(id=item['id'])
                shift.confirmed_starttime = None
                shift.confirmed_endtime = None
                shift.save()

        return JsonResponse({'success': True})

    return JsonResponse({'success': False})




def copy_shifts(request):
    if request.method == 'POST':
        # 全ての ShiftPreference オブジェクトを取得
        preferences = ShiftPreference.objects.all()
        
        for preference in preferences:
            # confirmed_starttime と confirmed_endtime に値をコピー
            preference.confirmed_starttime = preference.starttime
            preference.confirmed_endtime = preference.endtime
            preference.save()

        return JsonResponse({'success': True})

    return JsonResponse({'success': False}, status=400)

# Superuserのみアクセス可能にするデコレータ
def superuser_required(view_func):
    decorated_view_func = user_passes_test(
        lambda u: u.is_superuser,
        login_url='/login/',  # 権限がないユーザーをリダイレクトするURL
        redirect_field_name=None
    )(view_func)
    return decorated_view_func


@superuser_required
def shift_management_view(request):
    # シフト希望データを取得
    preferences = ShiftPreference.objects.all()
    # スタッフデータを取得
    staff = Staff.objects.all()

    # シフト開始・終了時間を日付と組み合わせる
    for preference in preferences:
        # starttime または endtime が None の場合はスキップ
        if preference.starttime and preference.endtime:
            # date フィールドと時間を組み合わせて confirmed_starttime を作成
            preference.starttime = datetime.combine(preference.date, preference.starttime)
            # date フィールドと時間を組み合わせて confirmed_endtime を作成
            preference.endtime = datetime.combine(preference.date, preference.endtime)

    # コンテキストにデータを渡す
    context = {
        'preferences': preferences,
        'staff': staff,
    }

    return render(request, 'shiftgenerator/shift_management_view.html', context)

def shift_management(request):
    # シフト希望データを取得し、confirmed_starttime と confirmed_endtime が None でないものに限定
    preferences = ShiftPreference.objects.exclude(confirmed_starttime__isnull=True, confirmed_endtime__isnull=True)
    # スタッフデータを取得
    staff = Staff.objects.all()

    # シフト開始・終了時間を日付と組み合わせる
    for preference in preferences:
        if preference.confirmed_starttime is not None and preference.confirmed_endtime is not None:
            # データベースから取得した時間を使用する
            start_time = preference.confirmed_starttime  # confirmed_starttimeを使用
            end_time = preference.confirmed_endtime      # confirmed_endtimeを使用
            # date フィールドと時間を組み合わせて confirmed_starttime を作成
            preference.confirmed_starttime = datetime.combine(preference.date, start_time)
            # date フィールドと時間を組み合わせて confirmed_endtime を作成
            preference.confirmed_endtime = datetime.combine(preference.date, end_time)

    # コンテキストにデータを渡す
    context = {
        'preferences': preferences,
        'staff': staff,
    }

    return render(request, 'shiftgenerator/shift_management.html', context)



@login_required
def shift_form(request):
    user = request.user
    staff_profile = getattr(user, 'staff_profile', None)
    
    if not staff_profile:
        return redirect('shiftgenerator:index')

    # シフトデータを取得
    shifts = ShiftPreference.objects.filter(staff=staff_profile)
    
    # シフト履歴を取得
    history = ShiftHistory.objects.filter(staff=staff_profile).order_by('-created_at')[:10]

    # データをJSON形式に変換
    events = []
    
    # シフトデータを元にイベントを生成
    for shift in shifts:
        if shift.holiday:

            # 休みの種類に応じて色を決定
            if shift.holiday.id == 1:  # 例: idが1の休み
                holiday_color = '#ff3d3d'  # 赤
            elif shift.holiday.id == 2:  # 例: idが2の休み
                holiday_color = '#12a8b3'  # 水色
            elif shift.holiday.id == 3:  # 例: idが3の休み
                holiday_color = '#ede100'  # 黄色
            else:
                holiday_color = 'red'  # デフォルトの色
        
            events.append({
                'title': shift.holiday.holiday_name,  # 休みの名前をタイトルとして使用
                'start': shift.date.isoformat() + 'T00:00:00',  # 一日の始まり
                'end': shift.date.isoformat() + 'T23:59:59',  # 一日の終わり
                'id': shift.id,  # シフトIDをそのまま使用
                'display': 'block',  # 通常のイベントとして表示
                'extendedProps': {  # extendedPropsに情報を追加
                'holiday': True,
                'holidayColor': holiday_color,# 休みの色を追加
                'starttime': None,
                'endtime': None
                }
            })
        elif shift.starttime and shift.endtime:
            events.append({
                'title': f'{shift.starttime.strftime("%H:%M")} - {shift.endtime.strftime("%H:%M")}',
                'id': shift.id,
                'start': f'{shift.date}T{shift.starttime.strftime("%H:%M:%S")}',
                'end': f'{shift.date}T{shift.endtime.strftime("%H:%M:%S")}',
                'starttime': shift.starttime.strftime("%H:%M"),
                'endtime': shift.endtime.strftime("%H:%M"),
                'backgroundColor': 'blue',  # 通常のシフトの色
                'extendedProps': {  # extendedPropsに情報を追加
                'holiday': False,
                'starttime': shift.starttime.strftime("%H:%M"),
                'endtime': shift.endtime.strftime("%H:%M")
                }
            })
   
    # 履歴が存在する場合のみJSON形式に変換
    history_list = []
    if history.exists():
        history_list = [{'start': h.starttime.strftime("%H:%M"), 'end': h.endtime.strftime("%H:%M")} for h in history]

    events_json = json.dumps(events)
    history_json = json.dumps(history_list)

    context = {
        'user': user,
        'events': events_json,
        'name': staff_profile.name,
        'username': user.username,
        'history': history_json if history_list else None
    }
    
    return render(request, 'shiftgenerator/shift_form.html', context)

@login_required
def shift_detail(request, shift_id):
    try:
        shift = ShiftPreference.objects.get(id=shift_id, staff=request.user.staff_profile)
    except ShiftPreference.DoesNotExist:
        return redirect('shiftgenerator:shift-form')  # シフトが存在しない場合のリダイレクト

    # Noneの値を'--'に置き換え
    shift_starttime = shift.starttime.strftime("%H:%M") if shift.starttime else '--'
    shift_endtime = shift.endtime.strftime("%H:%M") if shift.endtime else '--'
    holiday_name = shift.holiday.holiday_name if shift.holiday else '--'
    description = shift.description if shift.description else '--'

    if request.method == 'POST':
        # シフトを削除
        shift.delete()
        messages.success(request, 'シフトが正常に削除されました。')
        return redirect('shiftgenerator:shift-form')  # シフトフォームページにリダイレクト

    return render(request, 'shiftgenerator/shift_detail.html', {
        'shift': shift,
        'shift_starttime': shift_starttime,
        'shift_endtime': shift_endtime,
        'holiday_name': holiday_name,
        'description': description,
    })

# シフト履歴を取得するAPI
def get_shift_history(request):
    # 現在のユーザーのシフト履歴を取得
    histories = ShiftHistory.objects.filter(staff=request.user.staff_profile).order_by('-created_at')[:10]
    
    # シフト履歴をJSON形式に変換
    history_list = list(histories.values('starttime', 'endtime'))
    
    return JsonResponse(history_list, safe=False)

def register(request):
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)  # ユーザーをログインさせる
            messages.success(request, 'アカウントが正常に作成されました。')
            return redirect('shiftgenerator:index')  # 名前空間を含めたURLパターン名
        else:
            # フォームが無効な場合のエラーメッセージを表示
            messages.error(request, 'アカウントの作成に失敗しました。エラーを確認してください。')
    else:
        form = CustomUserCreationForm()
    
    return render(request, 'registration/register.html', {'form': form})


def get_holidays(request):
    holidays = Holiday.objects.values('id', 'holiday_name')  # 必要なフィールドを取得
    return JsonResponse(list(holidays), safe=False)

def login(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                auth_login(request, user)
                messages.success(request, 'ログインに成功しました。')
                return redirect('shiftgenerator:shift-form')
            else:
                messages.error(request, 'ユーザー名またはパスワードが無効です。')
        else:
            messages.error(request, 'ログイン中にエラーが発生しました。フォームを確認してください。')
    else:
        form = AuthenticationForm()
    return render(request, 'registration/login.html', {'form': form})

# ログ設定
logger = logging.getLogger(__name__)

# CSVファイルとモデルのディレクトリ
model_dir = os.path.join(settings.BASE_DIR, 'shiftgenerator/models/RandomForestmodels')
csv_file_path = os.path.join(settings.BASE_DIR, 'shiftgenerator/requ_shiftdata', 'req_shiftdata.csv')
staff_file_path = os.path.join(settings.BASE_DIR, 'shiftgenerator/staffdata', 'staff.csv')

# モデルのロード
with open(os.path.join(model_dir, 'rf_assigned_model.pkl'), 'rb') as f:
    rf_assigned = pickle.load(f)
with open(os.path.join(model_dir, 'rf_start_model.pkl'), 'rb') as f:
    rf_start = pickle.load(f)
with open(os.path.join(model_dir, 'rf_end_model.pkl'), 'rb') as f:
    rf_end = pickle.load(f)
with open(os.path.join(model_dir, 'rf_hours_model.pkl'), 'rb') as f:
    rf_hours = pickle.load(f)

print(type(rf_assigned))  # 確認

import sklearn
print(sklearn.__version__)

# 時間を分単位に変換する関数
def time_to_minutes(time_str):
    if pd.isna(time_str):
        return np.nan
    hours, minutes = map(int, time_str.split(':'))
    return hours * 60 + minutes

def minutes_to_time(minutes):
    if pd.isna(minutes):
        return "00:00"
    hours = int(minutes // 60)
    minutes = int(minutes % 60)
    return f"{hours:02}:{minutes:02}"

@csrf_exempt  # CSRFトークンの検証を無効化（セキュリティリスクに注意）
def shift_generate(request):
    if request.method == 'POST':
        try:
            # シフト希望のCSVファイルを読み込む
            new_data = pd.read_csv(csv_file_path)
            staff_df = pd.read_csv(staff_file_path)         


            # データの前処理
            weekday_map = {'月曜日': 0, '火曜日': 1, '水曜日': 2, '木曜日': 3, '金曜日': 4, '土曜日': 5, '日曜日': 6}
            new_data['day_of_week'] = new_data['day_of_week'].map(weekday_map)
            new_data['req_starttime_minutes'] = new_data['req_starttime'].apply(time_to_minutes)
            new_data['req_endtime_minutes'] = new_data['req_endtime'].apply(time_to_minutes)
            new_data = new_data[['staff_id', 'day_of_week', 'req_starttime_minutes', 'req_endtime_minutes']]

            # モデルによる予測
            assigned_predictions = rf_assigned.predict(new_data)
            start_predictions = rf_start.predict(new_data)
            end_predictions = rf_end.predict(new_data)
            hours_predictions = rf_hours.predict(new_data)

            # スタッフIDをキーにしてマージ
            new_data = new_data.merge(staff_df[['staff_id', 'name']], on='staff_id', how='left')
            print(new_data.columns)

            # 結果をDataFrameにまとめる
            results = pd.DataFrame({
                'スタッフID': new_data['staff_id'],
                'スタッフ名': new_data['name'],
                '曜日': new_data['day_of_week'].map({v: k for k, v in weekday_map.items()}),
                '希望開始時間': [minutes_to_time(x) for x in new_data['req_starttime_minutes']],
                '希望終了時間': [minutes_to_time(x) for x in new_data['req_endtime_minutes']],
                '予測されたシフトアサインメント': [f"{x:.1%}" for x in assigned_predictions],
                '予測された開始時間': [minutes_to_time(x) for x in start_predictions],
                '予測された終了時間': [minutes_to_time(x) for x in end_predictions],
                '予測された勤務時間': [minutes_to_time(x) for x in hours_predictions]
            })

            print(results.head())  # デバッグ用に結果を確認

            # 結果をセッションに保存
            request.session['shift_results'] = results.to_dict(orient='records')

            return redirect('shiftgenerator:shift-results')

        except Exception as e:
            logger.error(f"Error during prediction: {e}", exc_info=True)
            return HttpResponseServerError(f"Error during prediction: {e}")

    # GETリクエストの場合
    return render(request, 'shiftgenerator/shift_generate.html')


def shift_results(request):
    results = request.session.get('shift_results')
    if results:
        return render(request, 'shiftgenerator/shift_results.html', {'results': results})
    else:
        return redirect('shiftgenerator:shift-generate')


@login_required
def shift_register(request):
    # GETリクエストの場合、空のフォームを表示
    date = request.GET.get('date', '')
    user = request.user
    
    # ユーザーに関連付けられたスタッフ情報を取得
    staff_profile = getattr(user, 'staff_profile', None)
    if not staff_profile:
        return redirect('shiftgenerator:index')  # スタッフ情報がない場合のリダイレクト

    staff_name = staff_profile.name  # スタッフ名
    
    # 15分単位の時間スロットを生成
    time_slots = []
    base_time = datetime(2024, 1, 1, 8, 30)  # 8:30からスタート
    end_time = datetime(2024, 1, 1, 20, 0)  # 20:00まで

    while base_time <= end_time:
        time_slots.append(base_time.strftime('%H:%M'))
        base_time += timedelta(minutes=15)

    return render(request, 'shiftgenerator/shift_register.html', {
        'date': date,
        'time_slots': time_slots,
        'staff_name': staff_name,
        'username': user.username
    })


@login_required
@csrf_exempt
def new_register_shift(request):
    if request.method == 'POST':
        try:
            # JSONリクエストとPOSTリクエストの区別
            if request.content_type == 'application/json':
                data = json.loads(request.body)
                date = data.get('date')
                time_range = data.get('time_range')
            else:
                date = request.POST.get('date')
                time_range = f"{request.POST.get('start_time')} - {request.POST.get('end_time')}"

            starttime, endtime = time_range.split(' - ')

            # 日付と曜日を取得
            date_obj = datetime.strptime(date, '%Y-%m-%d')
            day_of_week = date_obj.weekday()  # 曜日 (0=月曜日, 6=日曜日)

            # DayOfWeek モデルから対応する曜日を取得
            try:
                day_of_week_instance = DayOfWeek.objects.get(day_number=day_of_week)
            except DayOfWeek.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'DayOfWeek インスタンスが見つかりませんでした'}, status=400)

            # 時間をdatetimeオブジェクトに変換
            starttime = datetime.strptime(starttime, '%H:%M').time()
            endtime = datetime.strptime(endtime, '%H:%M').time()

            # ユーザーのスタッフプロファイルを取得
            staff_profile = getattr(request.user, 'staff_profile', None)
            if not staff_profile:
                return redirect('shiftgenerator:index')

            # シフトの登録
            shift = ShiftPreference.objects.create(
                staff=staff_profile,
                starttime=starttime,
                endtime=endtime,
                date=date,
                day_of_week=day_of_week_instance  # ここでday_of_weekを正しくセット
            )
            print(f"ShiftPreference に登録: {shift}")

            # ShiftHistory に重複がない場合に保存
            if ShiftHistory.objects.filter(staff=staff_profile, starttime=starttime, endtime=endtime).count() == 0:
                ShiftHistory.objects.create(
                    staff=staff_profile,
                    starttime=starttime,
                    endtime=endtime
                )
                print("ShiftHistory に登録")
            else:
                print("ShiftHistory に重複するシフトがあるのでスキップしました")

            # 履歴が10個を超えた場合、古い履歴を削除
            if ShiftHistory.objects.filter(staff=staff_profile).count() > 10:
                oldest_history = ShiftHistory.objects.filter(staff=staff_profile).earliest('created_at')
                oldest_history.delete()
                print(f"古い ShiftHistory を削除: {oldest_history}")

            # JSONリクエストならJSONでレスポンス、通常のPOSTリクエストならリダイレクト
            if request.content_type == 'application/json':
                return JsonResponse({'success': True})
            else:
                return redirect('shiftgenerator:shift-form')

        except Exception as e:
            print(f"Error occurred: {e}")  # エラーログを出力
            if request.content_type == 'application/json':
                return JsonResponse({'success': False, 'error': str(e)})
            else:
                return redirect('shiftgenerator:shift-form')
    else:
        return redirect('shiftgenerator:shift-form')


def holiday_shift_register(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POSTメソッドのみ許可されています'}, status=405)

    try:
        data = json.loads(request.body)
        print(data)  # リクエストデータを確認

        # holiday_idを整数に変換
        holiday_id = int(data.get('holiday_id'))
        date_str = data.get('date')

        # 日付をパース
        date = parse_date(date_str)
        if not date:
            return JsonResponse({'success': False, 'error': '無効な日付形式です'}, status=400)

         # 曜日を取得 (0=月曜日, 6=日曜日)
        day_of_week_number = date.weekday()  # date.weekday()で曜日を取得
        
        # DayOfWeek モデルから対応する曜日を取得
        try:
            day_of_week_instance = DayOfWeek.objects.get(day_number=day_of_week_number)
        except DayOfWeek.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'DayOfWeek インスタンスが見つかりませんでした'}, status=400)

        # ユーザーのスタッフプロファイルを取得
        staff_profile = getattr(request.user, 'staff_profile', None)
        if not staff_profile:
            return JsonResponse({'success': False, 'error': 'スタッフプロファイルが見つかりません'}, status=400)

        # Holiday モデルから選択された休みを取得
        try:
            holiday_instance = Holiday.objects.get(id=holiday_id)
        except Holiday.DoesNotExist:
            return JsonResponse({'success': False, 'error': '選択された休みが見つかりませんでした'}, status=400)

        # シフト希望を登録（休み）
        ShiftPreference.objects.create(
            staff=staff_profile,
            date=date,
            holiday=holiday_instance,  # 休みの種類を登録
            day_of_week=day_of_week_instance  # 曜日情報を登録
        )

        return JsonResponse({'success': True})
    except Exception as e:
        print(f"Error: {str(e)}")  # エラーメッセージをコンソールに出力
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def history_shift_register(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            starttime_str = data.get('starttime')
            endtime_str = data.get('endtime')
            date_str = data.get('date')

            # 文字列から時間と日付をパース
            starttime = parse_time(starttime_str)
            endtime = parse_time(endtime_str)
            date = parse_date(date_str)

            if not (starttime and endtime and date):
                return JsonResponse({'success': False, 'error': '無効な日付または時間形式です'}, status=400)

            # 日付と曜日を取得
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            day_of_week = date_obj.weekday()  # 曜日 (0=月曜日, 6=日曜日)

            # DayOfWeek モデルから対応する曜日を取得
            try:
                day_of_week_instance = DayOfWeek.objects.get(day_number=day_of_week)
            except DayOfWeek.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'DayOfWeek インスタンスが見つかりませんでした'}, status=400)

            # ユーザーのスタッフプロファイルを取得
            staff_profile = getattr(request.user, 'staff_profile', None)
            if not staff_profile:
                return JsonResponse({'success': False, 'error': 'スタッフプロファイルが見つかりません'}, status=400)

            # シフトの登録
            ShiftPreference.objects.create(
                staff=staff_profile,
                starttime=starttime,
                endtime=endtime,
                date=date,
                day_of_week=day_of_week_instance
            )

            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

def get_updated_events(request):
    user = request.user
    staff_profile = getattr(user, 'staff_profile', None)
    
    if not staff_profile:
        return JsonResponse([], safe=False)

    shifts = ShiftPreference.objects.filter(staff=staff_profile)
    events = []

    for shift in shifts:
        if shift.holiday:
            # 休みのイベント
            events.append({
                'title': shift.holiday.holiday_name,  # 休みの名前をタイトルとして使用
                'start': shift.date.isoformat() + 'T00:00:00',  # 一日の始まり
                'end': shift.date.isoformat() + 'T23:59:59',  # 一日の終わり
                'id': shift.id,  # シフトIDをそのまま使用
                'backgroundColor': 'red',  # 休みの色
                'display': 'block'  # 通常のイベントとして表示
            })
        elif shift.starttime and shift.endtime:
            # 通常のシフトイベント
            events.append({
                'title': f'{shift.starttime.strftime("%H:%M")} - {shift.endtime.strftime("%H:%M")}',
                'id': shift.id,
                'start': f'{shift.date}T{shift.starttime.strftime("%H:%M:%S")}',
                'end': f'{shift.date}T{shift.endtime.strftime("%H:%M:%S")}',
                'starttime': shift.starttime.strftime("%H:%M"),
                'endtime': shift.endtime.strftime("%H:%M"),
                'backgroundColor': 'blue'  # 通常のシフトの色
            })
    
    return JsonResponse(events, safe=False)