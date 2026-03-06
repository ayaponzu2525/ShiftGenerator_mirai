from django.http import HttpResponse, JsonResponse, HttpResponseServerError, HttpResponseRedirect, HttpResponseForbidden, HttpResponseNotAllowed
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction, IntegrityError
from django.conf import settings
from django.contrib.auth import login as auth_login, authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib import messages
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time, parse_datetime
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.timezone import now, localdate
from django.db.models import Max

import pandas as pd
import pickle
import os
import numpy as np
import logging
import pytz
import json
import traceback
from datetime import datetime, date, timedelta, time
import csv
from django.views.decorators.http import require_POST, require_GET

from .forms import CustomUserCreationForm, ShiftPreferenceForm, ExcelTemplateUploadForm, StaffExcelCodeFormSet
from .models import ShiftPreference, Staff, DayOfWeek, ShiftRegisterAssignment, ShiftHistory, Holiday, Skill, StaffSkill, ShiftSubmissionPeriod, ShiftSubmission, ExcelExportTemplate, PublishedRegisterAssignment
from django.contrib.auth.hashers import make_password
from django.contrib import messages
from django.db.models import Exists, OuterRef, Q, F, Prefetch
from shiftgenerator.utils.utils import close_expired_periods
import openpyxl
from openpyxl.styles import PatternFill
from openpyxl.styles import Alignment
from io import BytesIO
import re
from datetime import date as dt_date


# === 店舗営業時間（バックエンド側） ===
BUSINESS_OPEN_TIME = time(8, 0)   # 08:00
BUSINESS_CLOSE_TIME = time(20, 0) # 20:00


def index(request):
    return render(request, 'shiftgenerator/home.html')

@user_passes_test(lambda u: u.is_superuser)
def staff_management(request):
    show_all = request.GET.get('show') == 'all'
    staff_list = Staff.objects.all() if show_all else Staff.objects.filter(is_active=True)
    return render(request, 'staff_admin/staff_management.html', {
        'staff_list': staff_list,
        'show_all': show_all,
    })

def get_display_staff_for_date(target_date):
    """
    表示対象スタッフ：
      - is_active=True は全員
      - is_active=False は、その日に confirmed シフトがある人だけ
    """
    active_staff = Staff.objects.filter(is_active=True)

    inactive_staff = Staff.objects.filter(is_active=False).filter(
        Exists(
            ShiftPreference.objects.filter(
                staff=OuterRef('pk'),
                date=target_date,
                confirmed_starttime__isnull=False,
                confirmed_endtime__isnull=False,
            )
        )
    )

    # 合体＆ID順（好みでname順でもOK）
    staff = sorted(list(active_staff) + list(inactive_staff), key=lambda s: s.id)
    return staff

@user_passes_test(lambda u: u.is_superuser)
def staff_disable(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    staff.is_active = False
    staff.save()
    messages.success(request, f'{staff.name} を無効化しました')
    return redirect('shiftgenerator:staff-management')

@user_passes_test(lambda u: u.is_superuser)
def staff_skill(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    skills = Skill.objects.all()
    if request.method == 'POST':
        skill_ids = request.POST.getlist('skills')
        # 既存を全削除→再追加の単純方式
        StaffSkill.objects.filter(staff=staff).delete()
        for skill_id in skill_ids:
            StaffSkill.objects.create(staff=staff, skill_id=skill_id)
        messages.success(request, 'スキルを更新しました')
        return redirect('shiftgenerator:staff-management')
    # 登録済みスキルID
    owned = StaffSkill.objects.filter(staff=staff).values_list('skill_id', flat=True)
    return render(request, 'staff_admin/staff_skill.html', {'staff': staff, 'skills': skills, 'owned': owned})

@user_passes_test(lambda u: u.is_superuser)
def staff_password(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    if request.method == 'POST':
        new_pass = request.POST.get('password')
        if staff.custom_user:
            staff.custom_user.set_password(new_pass)
            staff.custom_user.save()
            messages.success(request, 'パスワードを変更しました')
        return redirect('shiftgenerator:staff-management')
    return render(request, 'staff_admin/staff_password.html', {'staff': staff})

@user_passes_test(lambda u: u.is_superuser)
def staff_toggle_active(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    staff.is_active = not staff.is_active  # トグル切替
    staff.save()
    return redirect('shiftgenerator:staff-management')



@transaction.atomic  # 途中で失敗したらロールバック
def save_shifts(request):
    if request.method != "POST":
        return JsonResponse({'success': False})

    data     = json.loads(request.body)
    changes  = data.get('changes', [])
    
    # --- フェーズ分割 ---
    shift_phase = []  # add/move/update/remove（シフト）
    reg_phase   = []  # update_reg/remove_reg（レジ）
    for ch in changes:
        if ch['action'] in ('update_reg', 'remove_reg'):
            reg_phase.append(ch)
        else:
            shift_phase.append(ch)
    
    
    def parse_time_flex_to_local_time(s):
        """ISO(…Z/±hh:mm) or HH:MM[:SS] を「ローカル時刻」の time に揃える"""
        if not s:
            return None
        s = str(s)
        try:
            # ISO → aware → ローカルTZへ → time
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                # 明示TZ無いISOならローカルTZとして扱う
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            else:
                dt = dt.astimezone(timezone.get_current_timezone())
            return dt.time()
        except Exception:
            t = parse_time(s)  # "HH:MM" or "HH:MM:SS"
            return t

    # === 1) 先にシフトを保存 ===
    for change in changes:
        action = change['action']
        item   = change['item']

        # ★ wish- アイテムは完全スキップ（保険）
        if str(item.get('id', '')).startswith('wish-'):
            continue

        # ISO → naive → aware → 現地タイムゾーンへ
        start_dt = timezone.make_aware(
            datetime.fromisoformat(item['start'].replace('Z', '')),
            pytz.UTC
        ).astimezone(timezone.get_current_timezone())

        end_dt   = timezone.make_aware(
            datetime.fromisoformat(item['end'].replace('Z', '')),
            pytz.UTC
        ).astimezone(timezone.get_current_timezone())

        shift_date  = start_dt.date()
        start_time  = start_dt.time()
        end_time    = end_dt.time()
        new_staff   = item['group']          # ドロップ先のスタッフ ID

        if action == 'add':
            # --- 新しい確定シフトをまるごと作成 ---
            day_obj = DayOfWeek.objects.get(day_number=shift_date.weekday())
            ShiftPreference.objects.create(
                staff_id            = new_staff,
                date                = shift_date,
                confirmed_starttime = start_time,
                confirmed_endtime   = end_time,
                day_of_week         = day_obj
            )

        elif action in ('move', 'update'):
            shift = ShiftPreference.objects.select_for_update().get(id=item['id'])

            # ===== スタッフが変わった？ =====
            if shift.staff_id != new_staff:
                # A) 元レコード → 希望だけ残す
                shift.confirmed_starttime = None
                shift.confirmed_endtime   = None
                shift.save()

                # B) 新レコード → 確定だけ入れる
                day_obj = DayOfWeek.objects.get(day_number=shift_date.weekday())
                ShiftPreference.objects.create(
                    staff_id            = new_staff,
                    date                = shift_date,
                    confirmed_starttime = start_time,
                    confirmed_endtime   = end_time,
                    day_of_week         = day_obj
                )
            else:
                # 同じスタッフ内の時間変更なら confirmed_* だけ更新
                shift.confirmed_starttime = start_time
                shift.confirmed_endtime   = end_time
                shift.save()

        elif action == 'remove':
            # confirmed_* を空にして「希望だけ残す」
            shift = ShiftPreference.objects.select_for_update().get(id=item['id'])
            shift.confirmed_starttime = None
            shift.confirmed_endtime   = None
            shift.save()
            
    # === 2) 次にレジを保存（ローカル時刻に正規化） ===
    for change in reg_phase:
        action = change['action']
        item   = change['item']

        if action == 'update_reg':
            reg_pk = str(item.get('id')).replace('reg-','')
            reg = ShiftRegisterAssignment.objects.select_for_update().get(pk=reg_pk)

            start_t = parse_time_flex_to_local_time(item.get('start') or item.get('register_start_time'))
            end_t   = parse_time_flex_to_local_time(item.get('end')   or item.get('register_end_time'))
            if start_t is None or end_t is None:
                return JsonResponse({'success': False, 'error': f'invalid time: {item.get("start")}, {item.get("end")}'}, status=400)

            reg.register_start_time = start_t
            reg.register_end_time   = end_t
            if item.get('shift_id'):        reg.shift_id = int(item['shift_id'])
            if item.get('register_number') is not None:
                reg.register_number = int(item['register_number'])
            reg.save()

        elif action == 'remove_reg':
            reg_pk = str(item.get('id')).replace('reg-','')
            ShiftRegisterAssignment.objects.filter(pk=reg_pk).delete()


    return JsonResponse({'success': True})

def time_overlap(s1, e1, s2, e2):
    """
    ２つの時間帯が重複しているか
    端点（e1 == s2 など）は重複とみなさない
    """
    return (e1 > s2) and (e2 > s1)


@require_POST
@user_passes_test(lambda u: u.is_superuser)
def save_register_assignments(request):
    try:
        data = json.loads(request.body)
        assignments = data.get('assignments', [])
        shift_id_in_body = data.get('shift_id')                 # ← 空配列用
        shift_windows = {}

        def attach_window(raw_shift_id, entry):
            if entry is None or not isinstance(entry, dict):
                return
            sid = raw_shift_id
            if sid is None:
                return
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return
            start_raw = entry.get('start')
            end_raw = entry.get('end')
            try:
                start_time = datetime.strptime(start_raw, '%H:%M').time() if start_raw else None
                end_time = datetime.strptime(end_raw, '%H:%M').time() if end_raw else None
            except (ValueError, TypeError):
                return
            shift_windows[sid] = {'start': start_time, 'end': end_time}

        raw_windows = data.get('shift_windows')
        if isinstance(raw_windows, dict):
            for key, value in raw_windows.items():
                if isinstance(value, dict):
                    attach_window(value.get('shift_id', key), value)
        single_window = data.get('shift_window')
        if isinstance(single_window, dict):
            attach_window(single_window.get('shift_id', shift_id_in_body), single_window)


        # ---------- ① assignments が空なら、指定 shift_id を全削除 ----------
        if not assignments:
            if shift_id_in_body:
                ShiftRegisterAssignment.objects.filter(shift_id=shift_id_in_body).delete()
            return JsonResponse({'success': True, 'logs': [], 'info': '全削除のみ実施'})
        
        
        # ======== STEP 0: 正規化（シフト境界でクランプ → 完全非重複は除外 or 400）========
        # 0-1) 今回触る shift をまとめて取得（この後ずっと使う）
        shift_ids = {a['shift_id'] for a in assignments}
        shift_map = ShiftPreference.objects.in_bulk(shift_ids)

        norm = []     # 正規化後のレコード（この後は assignments の代わりにコレだけを使う）
        logs = []     # 既存ログをここで使いまわす（最後に返す）
        
        for a in assignments:
            shift_id = a['shift_id']
            shift = shift_map[shift_id]

            # 文字列→time
            s_time = datetime.strptime(a['register_start_time'], '%H:%M').time()
            e_time = datetime.strptime(a['register_end_time'],   '%H:%M').time()

            # シフト枠でクランプ（同日time同士の比較なのでそのままOK）
            clamp_start = shift.confirmed_starttime or shift.starttime
            clamp_end = shift.confirmed_endtime or shift.endtime
            window = shift_windows.get(shift_id)
            if window:
                if window.get('start'):
                    clamp_start = window['start']
                if window.get('end'):
                    clamp_end = window['end']
            cut = False
            if clamp_start and s_time < clamp_start:
                s_time, cut = clamp_start, True
            if clamp_end and e_time > clamp_end:
                e_time, cut = clamp_end, True

            # 幅が無い/逆転 → 完全非重複なのでスキップ（要件次第で 400 にしてもOK）
            if s_time >= e_time:
                # スキップ理由をログしたい場合はここで logs.append してもよい
                continue

            if cut:
                logs.append({
                    'shift_id': shift.id,
                    'register_number': a['register_number'],
                    'message': 'シフト外をカットして保存（サーバ側）'
                })

            norm.append({
                'shift_id': shift_id,
                'register_number': a['register_number'],
                'register_start_time': s_time,  # ここは time 型
                'register_end_time':   e_time,  # ここも time 型
            })

        # 正規化の結果、保存対象が消えた場合は古い割当を削除して終了
        if not norm:
            for sid in shift_ids:
                ShiftRegisterAssignment.objects.filter(shift_id=sid).delete()
            return JsonResponse({'success': True, 'logs': logs, 'info': '正規化後に保存対象なし（全削除）'})


       # ---------- ② 送信内容内での重複チェック（正規化後の norm を使用） ----------
        for i, a in enumerate(norm):
            a_num   = a['register_number']
            a_start = a['register_start_time']
            a_end   = a['register_end_time']
            for b in norm[i+1:]:
                if b['register_number'] != a_num:
                    continue
                b_start = b['register_start_time']
                b_end   = b['register_end_time']
                if time_overlap(a_start, a_end, b_start, b_end):
                    msg = (
                        f'レジ{a_num} が {a_start.strftime("%H:%M")}〜{a_end.strftime("%H:%M")} と '
                        f'{b_start.strftime("%H:%M")}〜{b_end.strftime("%H:%M")} で重複しています'
                    )
                    return JsonResponse({'success': False, 'error': msg})

        # ---------- ②-2 同一シフト内でのレジ重複チェック（番号が異なる場合も禁止） ----------
        per_shift = {}
        for record in norm:
            per_shift.setdefault(record['shift_id'], []).append(record)

        for sid, recs in per_shift.items():
            shift_obj = shift_map.get(sid)
            staff_name = getattr(getattr(shift_obj, 'staff', None), 'name', 'スタッフ') if shift_obj else 'スタッフ'
            date_label = getattr(shift_obj, 'date', '')
            ordered = sorted(recs, key=lambda r: (r['register_start_time'], r['register_end_time']))
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    if a['register_number'] == b['register_number']:
                        continue  # 同番号のチェックは既に済み
                    if time_overlap(a['register_start_time'], a['register_end_time'],
                                    b['register_start_time'], b['register_end_time']):
                        msg = (
                            f'{date_label} {staff_name} のシフト内で ' 
                            f'レジ{a["register_number"]} {a["register_start_time"].strftime("%H:%M")}〜{a["register_end_time"].strftime("%H:%M")} と ' 
                            f'レジ{b["register_number"]} {b["register_start_time"].strftime("%H:%M")}〜{b["register_end_time"].strftime("%H:%M")} が重複しています。'
                        )
                        return JsonResponse({'success': False, 'error': msg})

        # ---------- ③ 既存 DB との重複チェック（正規化後の norm を使用） ----------
        # shift_map は STEP 0 で取得済み
        for a in norm:
            shift_id        = a['shift_id']
            register_number = a['register_number']
            new_start = a['register_start_time']
            new_end   = a['register_end_time']
            shift_obj = shift_map[shift_id]

            overlaps = (
                ShiftRegisterAssignment.objects
                .filter(
                    shift__date=shift_obj.date,
                    register_number=register_number
                )
                .exclude(shift_id=shift_id)  # 自分自身は除外
            )
            for o in overlaps:
                o_start = o.register_start_time
                o_end   = o.register_end_time
                if time_overlap(new_start, new_end, o_start, o_end):
                    msg = (
                        f'{shift_obj.date} のレジ{register_number} は '
                        f'既に他スタッフ({o.shift.staff.name})で '
                        f'{o_start.strftime("%H:%M")}〜{o_end.strftime("%H:%M")} が割当済みです'
                    )
                    return JsonResponse({'success': False, 'error': msg})

        # ---------- ④ 登録処理（正規化後の norm を保存） ----------
        for sid in shift_ids:
            ShiftRegisterAssignment.objects.filter(shift_id=sid).delete()

        for a in norm:
            shift  = shift_map[a['shift_id']]
            reg_no = a['register_number']
            s_time = a['register_start_time']
            e_time = a['register_end_time']

            # ここでのカットは既に STEP 0 済みなので不要
            ShiftRegisterAssignment.objects.create(
                shift=shift,
                register_number=reg_no,
                register_start_time=s_time,
                register_end_time=e_time
            )

        return JsonResponse({'success': True, 'logs': logs})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@user_passes_test(lambda u: u.is_superuser)
def save_shift_and_registers(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': '無効なリクエストです。'}, status=400)

    changes = payload.get('changes') or []
    registers_payload = payload.get('registers_by_shift') or {}

    if not isinstance(changes, list) or not isinstance(registers_payload, dict):
        return JsonResponse({'success': False, 'error': '無効なリクエストです。'}, status=400)

    shift_changes = [c for c in changes if c.get('action') not in ('update_reg', 'remove_reg')]

    normalized_shifts = []
    normalized_registers = {}
    versions = {}

    def parse_iso_to_local(value):
        dt = datetime.fromisoformat(str(value).replace('Z', ''))
        if dt.tzinfo is None:
            dt = timezone.make_aware(dt, pytz.UTC)
        return dt.astimezone(timezone.get_current_timezone())
    
    def get_day_obj_safe(d):
        try:
            return DayOfWeek.objects.get(day_number=d.weekday())
        except DayOfWeek.DoesNotExist:
            return None  # day_of_week が必須ならここで ValidationError を返す運用でもOK

    # === 営業時間にシフト時間を丸める（サーバー側） ===
    def clamp_shift_times_to_business_hours(start_time, end_time):
        """
        start_time, end_time: datetime.time
        戻り値: (clamped_start, clamped_end)
        """
        if start_time is None or end_time is None:
            return start_time, end_time

        # まず単純に 8:00〜20:00 に切り詰める
        s = max(start_time, BUSINESS_OPEN_TIME)
        e = min(end_time,   BUSINESS_CLOSE_TIME)

        # 万一 0分以下になった場合の保険（普通の操作ではほぼ来ない前提）
        if e <= s:
            base_date = date.today()
            # とりあえず「1時間だけ確保」しておく
            s_dt = datetime.combine(base_date, s)
            e_dt = s_dt + timedelta(hours=1)
            # 20:00 を超えないように再度 clamp
            if e_dt.time() > BUSINESS_CLOSE_TIME:
                e_dt = datetime.combine(base_date, BUSINESS_CLOSE_TIME)
                s_dt = e_dt - timedelta(hours=1)
            s, e = s_dt.time(), e_dt.time()

        return s, e
    try:
        with transaction.atomic():
            touched_shift_ids = set()
            shift_map = {}

            for change in shift_changes:
                action = change.get('action')
                item = change.get('item') or {}
                item_id = item.get('id')

                if str(item_id).startswith('wish-'):
                    continue

                start_iso = item.get('start')
                end_iso = item.get('end')
                start_time = end_time = None
                shift_date = None

                if action in ('add', 'move', 'update'):
                    if not start_iso or not end_iso:
                        return JsonResponse({'success': False, 'error': 'シフトの開始・終了時刻が不足しています。'}, status=400)
                    try:
                        start_dt = parse_iso_to_local(start_iso)
                        end_dt = parse_iso_to_local(end_iso)
                    except Exception:
                        return JsonResponse({'success': False, 'error': 'シフトの時間形式が不正です。'}, status=400)

                    shift_date = start_dt.date()
                    start_time = start_dt.time()
                    end_time = end_dt.time()
                    
                    # ★ サーバー側でも営業時間に自動クランプ
                    start_time, end_time = clamp_shift_times_to_business_hours(start_time, end_time)

                new_staff = item.get('group')
                if new_staff is not None:
                    try:
                        new_staff = int(new_staff)
                    except (TypeError, ValueError):
                        return JsonResponse({'success': False, 'error': 'スタッフIDが不正です。'}, status=400)

                if action == 'add':
                    if new_staff is None:
                        return JsonResponse({'success': False, 'error': 'スタッフ情報が不足しています。'}, status=400)
                    day_obj = get_day_obj_safe(shift_date)
                    shift = ShiftPreference.objects.create(
                        staff_id=new_staff,
                        date=shift_date,
                        confirmed_starttime=start_time,
                        confirmed_endtime=end_time,
                        day_of_week=day_obj
                    )
                    shift_map[shift.id] = shift
                    touched_shift_ids.add(shift.id)

                elif action in ('move', 'update'):
                    if item_id is None:
                        continue
                    try:
                        shift_id = int(item_id)
                    except (TypeError, ValueError):
                        return JsonResponse({'success': False, 'error': 'シフトIDが不正です。'}, status=400)

                    shift = ShiftPreference.objects.select_for_update().get(id=shift_id)
                    shift_map[shift.id] = shift
                    touched_shift_ids.add(shift.id)

                    if new_staff is not None and shift.staff_id != new_staff:
                        shift.confirmed_starttime = None
                        shift.confirmed_endtime = None
                        shift.save()

                        day_obj = get_day_obj_safe(shift_date)
                        new_shift = ShiftPreference.objects.create(
                            staff_id=new_staff,
                            date=shift_date,
                            confirmed_starttime=start_time,
                            confirmed_endtime=end_time,
                            day_of_week=day_obj
                        )
                        shift_map[new_shift.id] = new_shift
                        touched_shift_ids.add(new_shift.id)
                    else:
                        shift.confirmed_starttime = start_time
                        shift.confirmed_endtime = end_time
                        shift.save()

                elif action == 'remove' and item_id is not None:
                    try:
                        shift_id = int(item_id)
                    except (TypeError, ValueError):
                        return JsonResponse({'success': False, 'error': 'シフトIDが不正です。'}, status=400)
                    shift = ShiftPreference.objects.select_for_update().get(id=shift_id)
                    shift_map[shift.id] = shift
                    shift.confirmed_starttime = None
                    shift.confirmed_endtime = None
                    shift.save()
                    touched_shift_ids.add(shift.id)

            register_shift_ids = set()
            for key in registers_payload.keys():
                try:
                    register_shift_ids.add(int(key))
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': 'レジ情報のシフトIDが不正です。'}, status=400)

            touched_shift_ids.update(register_shift_ids)
            all_shift_ids = set(touched_shift_ids) | register_shift_ids
            if all_shift_ids:
                db_shifts = ShiftPreference.objects.select_for_update().in_bulk(all_shift_ids)
                shift_map.update(db_shifts)

            for shift_id in register_shift_ids:
                shift = shift_map.get(shift_id)
                if not shift:
                    return JsonResponse({'success': False, 'error': f'シフト {shift_id} が見つかりません。'}, status=400)

                assignments = registers_payload.get(str(shift_id)) or []
                norm_list = []

                clamp_start = shift.confirmed_starttime or getattr(shift, 'starttime', None)
                clamp_end   = shift.confirmed_endtime   or getattr(shift, 'endtime', None)


                for entry in assignments:
                    try:
                        reg_no = int(entry.get('register_number'))
                        start_str = entry.get('register_start_time')
                        end_str = entry.get('register_end_time')
                        start_t = datetime.strptime(start_str, '%H:%M').time()
                        end_t = datetime.strptime(end_str, '%H:%M').time()
                    except (TypeError, ValueError):
                        return JsonResponse({'success': False, 'error': 'レジ時間の形式が不正です。'}, status=400)

                    if clamp_start and start_t < clamp_start:
                        start_t = clamp_start
                    if clamp_end and end_t > clamp_end:
                        end_t = clamp_end
                    if start_t >= end_t:
                        continue
                    start_dt = datetime.combine(shift.date, start_t)
                    end_dt   = datetime.combine(shift.date, end_t)
                    duration_minutes = int((end_dt - start_dt).total_seconds() // 60)
                    if duration_minutes < 5:
                        continue

                    norm_list.append({
                        'shift_id': shift_id,
                        'register_number': reg_no,
                        'start': start_t,
                        'end': end_t,
                    })

                for i, a in enumerate(norm_list):
                    for b in norm_list[i + 1:]:
                        if a['register_number'] != b['register_number']:
                            continue
                        if time_overlap(a['start'], a['end'], b['start'], b['end']):
                            staff_name = getattr(getattr(shift, 'staff', None), 'name', 'スタッフ')
                            msg = (
                                f'{shift.date} {staff_name} のレジ重複：'
                                f'レジ{a["register_number"]}（{a["start"].strftime("%H:%M")}〜{a["end"].strftime("%H:%M")}）と '
                                f'レジ{b["register_number"]}（{b["start"].strftime("%H:%M")}〜{b["end"].strftime("%H:%M")}）が同時刻です'
                            )
                            conflict = {
                                'shift_id': shift_id,
                                'type': 'register_number_overlap',
                                'a': {
                                    'number': a['register_number'],
                                    'start': a['start'].strftime('%H:%M'),
                                    'end': a['end'].strftime('%H:%M'),
                                },
                                'b': {
                                    'number': b['register_number'],
                                    'start': b['start'].strftime('%H:%M'),
                                    'end': b['end'].strftime('%H:%M'),
                                },
                            }
                            return JsonResponse({'success': False, 'error': msg, 'conflicts': [conflict]})

                ordered = sorted(norm_list, key=lambda x: (x['start'], x['end']))
                for i, a in enumerate(ordered):
                    for b in ordered[i + 1:]:
                        if time_overlap(a['start'], a['end'], b['start'], b['end']):
                            staff_name = getattr(getattr(shift, 'staff', None), 'name', 'スタッフ')
                            msg = (
                                f'{shift.date} {staff_name} のレジ重複：'
                                f'レジ{a["register_number"]}（{a["start"].strftime("%H:%M")}〜{a["end"].strftime("%H:%M")}）と '
                                f'レジ{b["register_number"]}（{b["start"].strftime("%H:%M")}〜{b["end"].strftime("%H:%M")}）が同時刻です'
                            )
                            conflict = {
                                'shift_id': shift_id,
                                'type': 'intra_staff_overlap',
                                'a': {
                                    'number': a['register_number'],
                                    'start': a['start'].strftime('%H:%M'),
                                    'end': a['end'].strftime('%H:%M'),
                                },
                                'b': {
                                    'number': b['register_number'],
                                    'start': b['start'].strftime('%H:%M'),
                                    'end': b['end'].strftime('%H:%M'),
                                },
                            }
                            return JsonResponse({'success': False, 'error': msg, 'conflicts': [conflict]})

                # 他スタッフ（同一日・同一レジ番号）との重複チェック
                for record in norm_list:
                    qs = (
                        ShiftRegisterAssignment.objects
                        .filter(
                            shift__date=shift.date,
                            register_number=record['register_number'],
                        )
                        .exclude(shift_id=shift_id)
                        .select_related('shift')
                    )

                    for existed in qs:
                        if time_overlap(record['start'], record['end'],
                                        existed.register_start_time, existed.register_end_time):
                            other_staff = getattr(getattr(existed.shift, 'staff', None), 'name', 'スタッフ')
                            msg = (
                                f'{shift.date} のレジ{record["register_number"]} は '
                                f'{other_staff}'
                                f'（{existed.register_start_time.strftime("%H:%M")}〜{existed.register_end_time.strftime("%H:%M")}）と重複しています'
                            )
                            conflict = {
                                'shift_id': shift_id,
                                'type': 'register_number_conflict',
                                'a': {
                                    'number': record['register_number'],
                                    'start': record['start'].strftime('%H:%M'),
                                    'end': record['end'].strftime('%H:%M'),
                                },
                                'b': {
                                    'number': record['register_number'],
                                    'start': existed.register_start_time.strftime('%H:%M'),
                                    'end': existed.register_end_time.strftime('%H:%M'),
                                    'staff': other_staff,
                                },
                            }
                            return JsonResponse({'success': False, 'error': msg, 'conflicts': [conflict]})

                ShiftRegisterAssignment.objects.filter(shift_id=shift_id).delete()
                if norm_list:
                    ShiftRegisterAssignment.objects.bulk_create([
                        ShiftRegisterAssignment(
                            shift=shift,
                            register_number=rec['register_number'],
                            register_start_time=rec['start'],
                            register_end_time=rec['end']
                        ) for rec in norm_list
                    ])

                normalized_registers[str(shift_id)] = [
                    {
                        'register_number': rec['register_number'],
                        'register_start_time': rec['start'].strftime('%H:%M'),
                        'register_end_time': rec['end'].strftime('%H:%M'),
                    } for rec in norm_list
                ]

            for raw_id in registers_payload.keys():
                key = str(raw_id)
                if key not in normalized_registers:
                    normalized_registers[key] = []

            for shift_id in touched_shift_ids:
                shift = shift_map.get(shift_id)
                if not shift:
                    continue
                start_val = end_val = None
                if shift.confirmed_starttime and shift.confirmed_endtime:
                    start_val = datetime.combine(shift.date, shift.confirmed_starttime).isoformat(timespec='seconds')
                    end_val = datetime.combine(shift.date, shift.confirmed_endtime).isoformat(timespec='seconds')
                normalized_shifts.append({
                    'id': shift.id,
                    'start': start_val,
                    'end': end_val
                })

    except Exception as exc:
        logger.exception('save_shift_and_registers error')
        return JsonResponse({'success': False, 'error': '保存に失敗しました。もう一度お試しください。'})

    return JsonResponse({
        'success': True,
        'normalized': {
            'shifts': normalized_shifts,
            'registers_by_shift': normalized_registers,
        },
        'versions': versions
    })

@require_GET
@user_passes_test(lambda u: u.is_superuser)
def get_register_assignments(request):
    shift_id = request.GET.get('shift_id')
    try:
        shift = ShiftPreference.objects.get(id=shift_id)
        assignments = shift.register_assignments.all()

        data = [{
            'id': reg.id,
            'register_number': reg.register_number,
            'register_start_time': reg.register_start_time.strftime('%H:%M') if reg.register_start_time else None,
            'register_end_time': reg.register_end_time.strftime('%H:%M') if reg.register_end_time else None,
        } for reg in assignments]

        return JsonResponse({'assignments': data})
    except ShiftPreference.DoesNotExist:
        return JsonResponse({'assignments': []})

    
    
@require_POST
@user_passes_test(lambda u: u.is_superuser)
def delete_register_assignment(request):
    try:
        data = json.loads(request.body)
        reg_id = data.get('id')
        ShiftRegisterAssignment.objects.filter(id=reg_id).delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})




@csrf_exempt
def copy_shifts(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            start_date = parse_date(data.get('start_date'))
            end_date = parse_date(data.get('end_date'))
            staff_id = data.get('staff_id')

            if not start_date or not end_date:
                return JsonResponse({'success': False, 'message': '日付が不正です'}, status=400)

            query = ShiftPreference.objects.filter(
                date__range=(start_date, end_date),
                starttime__isnull=False,
                endtime__isnull=False
            )
            if staff_id:
                query = query.filter(staff_id=staff_id)

            for pref in query:
                pref.confirmed_starttime = pref.starttime
                pref.confirmed_endtime = pref.endtime
                pref.save()

            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)}, status=500)

    return JsonResponse({'success': False, 'message': 'POSTメソッドが必要です'}, status=400)


# Superuserのみアクセス可能にするデコレータ
def superuser_required(view_func):
    decorated_view_func = user_passes_test(
        lambda u: u.is_superuser,
        login_url='/login/',  # 権限がないユーザーをリダイレクトするURL
        redirect_field_name=None
    )(view_func)
    return decorated_view_func


def is_month_last(date):
    # その月の1日＋1か月−1日 ＝ 月末日
    next_month = date.replace(day=28) + timedelta(days=4)  # 絶対に翌月になる
    return date.day == (next_month - timedelta(days=next_month.day)).day

def guess_period_pattern(period):
    if not period:
        return "halfmonth"  # デフォルト
    s, e = period.start_date, period.end_date
    # 半月ごと
    if s.day == 1 and e.day == 15:
        return "halfmonth"
    if s.day == 16 and is_month_last(e):
        return "halfmonth"
    # 1か月ごと（1日〜月末）
    if s.day == 1 and is_month_last(e):
        return "month"
    # 1週間ごと
    if (e - s).days == 6:
        return "week"
    # それ以外はカスタム
    return "custom"


def is_overlap(start1, end1, start2, end2):
    return not (end1 < start2 or end2 < start1)

def has_duplicate_period(type_, start_date, end_date, start_time=None, end_time=None, exclude_id=None):
    # default/temporaryは相互に重複禁止
    if type_ in ['default', 'temporary']:
        # どちらかのtypeがdefaultかtemporaryなら、両方合わせて調べる
        qs = ShiftSubmissionPeriod.objects.filter(
            type__in=['default', 'temporary'],
            is_active=True
        )
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        for period in qs:
            if is_overlap(start_date, end_date, period.start_date, period.end_date):
                return True
        return False
    elif type_ == 'HELP':
        qs = ShiftSubmissionPeriod.objects.filter(type='HELP', is_active=True)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        for period in qs:
            if (start_date == period.start_date and
                end_date == period.end_date and
                start_time == period.start_time and
                end_time == period.end_time):
                return True
        return False
    return False

def make_help_label(start_date, start_time, end_time):
    # 例: 2024年06月05日：09:00～12:00
    label = f"{start_date.strftime('%Y年%m月%d日')}：{start_time.strftime('%H:%M')}～{end_time.strftime('%H:%M')}"
    return label

@user_passes_test(lambda u: u.is_superuser)
def shift_management_view(request):
    # ❶ 期限切れを自動停止
    close_expired_periods()

    # 期間指定パラメータ取得
    show_past = request.GET.get("show_past") == "1"
    past_from = request.GET.get("past_from")
    past_to   = request.GET.get("past_to")

    qs = ShiftSubmissionPeriod.objects.all().order_by('-start_date')
    active_periods   = qs.filter(is_active=True)
    inactive_periods = qs.filter(is_active=False)

    # ---- 停止中の絞り込み ----
    if show_past and (past_from or past_to):          # ← 両方空なら表示しない
        if past_from:
            inactive_periods = inactive_periods.filter(end_date__gte=past_from)
        if past_to:
            inactive_periods = inactive_periods.filter(end_date__lte=past_to)
    else:
        inactive_periods = inactive_periods.none()    # ← ここで空に


    # ❹ スタッフ一覧(有効スタッフのみ)
    staff_list = Staff.objects.filter(is_active=True)

    # ❺ 提出状況の集計
    selected_period = None
    submissions = {}
    period_id = request.GET.get("period_id")
    if period_id:
        selected_period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)
        print("SELECTED:", selected_period.id, selected_period.is_active)
        for s in staff_list:
            sub = (
                ShiftSubmission.objects
                .filter(staff=s, period=selected_period)
                .order_by('-updated_at')
                .first()
            )
            if sub:
                submissions[s.id] = sub

    # 新規作成処理
    if request.method == "POST":
        start_date = parse_date(request.POST.get("start_date"))
        end_date = parse_date(request.POST.get("end_date"))
        type_ = request.POST.get("type")
        label = request.POST.get("label", "")
        start_time = parse_time(request.POST.get("start_time"))
        end_time = parse_time(request.POST.get("end_time"))
        # auto_close_date は GET/POST 両方から受け、値がある時だけ parse
        auto_close_str = request.POST.get("auto_close_date") or request.GET.get("auto_close_date") or ""
        auto_close_date = parse_datetime(auto_close_str) if auto_close_str else None
        if auto_close_date is not None and timezone.is_naive(auto_close_date):
            auto_close_date = timezone.make_aware(auto_close_date, timezone.get_current_timezone())
        
        if type_ == "HELP":
            label = make_help_label(start_date, start_time, end_time)
        
        if not start_date or not end_date:
            messages.error(request, "開始日と終了日は必須です。")
            return redirect("shiftgenerator:shift-management-view")
        if end_date < start_date:
            messages.error(request, "終了日は開始日以降を指定してください。")
            return redirect("shiftgenerator:shift-management-view")

        # 時刻が設定されているモードだけ比較（どちらか未入力ならスキップ）
        if start_time and end_time and end_time <= start_time:
            messages.error(request, "終了時刻は開始時刻より後にしてください。")
            return redirect("shiftgenerator:shift-management-view")
        
        # ✅ デフォルトの時だけ重複チェック
        if type_ == "default" and has_duplicate_period(type_, start_date, end_date, start_time, end_time):
            url = reverse("shiftgenerator:shift-management-view")
            return redirect(f"{url}?open=create&dup=1")

        try:
            ShiftSubmissionPeriod.objects.create(
                label=label,
                start_date=start_date,
                end_date=end_date,
                type=type_,
                is_active=True,
                start_time=start_time,
                end_time=end_time,
                auto_close_date=auto_close_date,
            )
        except IntegrityError:
            messages.error(request, "同じ種類・同じ開始日/終了日の募集期間が既に存在します。")
            url = reverse("shiftgenerator:shift-management-view")
            return redirect(f"{url}?open=create")

        messages.success(request, "新しい募集期間を作成しました。")
        return redirect("shiftgenerator:shift-management-view")


    # ❼ 直近のデフォルト期間
    latest_period  = ShiftSubmissionPeriod.objects.filter(type="default").order_by('-end_date').first()
    latest_pattern = guess_period_pattern(latest_period)
    
    print("show_past=", show_past, "from=", past_from, "to=", past_to)
    print("inactive COUNT=", inactive_periods.count())
    
    # ✅ 集計用データ
    total_staff = staff_list.count()
    submitted_staff = len(submissions)
    submission_rate = (submitted_staff / total_staff) * 100 if total_staff > 0 else 0
    unsubmitted_staff = [s for s in staff_list if s.id not in submissions]
    # select表示用（受付中＋終了済み直近10件）
    active_periods_select = ShiftSubmissionPeriod.objects.filter(is_active=True).order_by('-start_date')
    recent_inactive_select = ShiftSubmissionPeriod.objects.filter(is_active=False).order_by('-start_date')[:5]

    select_periods = list(active_periods_select) + list(recent_inactive_select)

    # 「選択中が古すぎてリストにいない」問題の保険
    if selected_period and selected_period not in select_periods:
        select_periods = [selected_period] + select_periods

    # ❽ テンプレートへ渡す
    return render(request, "shiftgenerator/shift_management_view.html", {
        "active_periods": active_periods,
        "inactive_periods": inactive_periods,
        "show_past": show_past,
        "past_from": past_from,
        "past_to": past_to,
        "staff_list": staff_list,
        "selected_period": selected_period,
        "submissions": submissions,

        "total_staff": total_staff,
        "submitted_staff": submitted_staff,
        "submission_rate": submission_rate,
        "unsubmitted_staff": unsubmitted_staff,

        "select_periods": select_periods,
        "latest_period": latest_period,
        "latest_pattern": latest_pattern,
    })

@require_POST
@user_passes_test(lambda u: u.is_superuser)
def shift_period_edit(request, id):
    period = get_object_or_404(ShiftSubmissionPeriod, id=id)
    type_ = request.POST['type']
    start_date = parse_date(request.POST.get('start_date'))
    end_date = parse_date(request.POST.get('end_date'))
    start_time = parse_time(request.POST.get('start_time'))
    end_time = parse_time(request.POST.get('end_time'))
    auto_close_date_str = request.POST.get("auto_close_date")
    if auto_close_date_str:
        auto_close_date = parse_datetime(auto_close_date_str)
    else:
        auto_close_date = None


    
    if type_ == "HELP":
        label = make_help_label(start_date, start_time, end_time)
        period.label = label
    else:
        period.label = request.POST.get("label", "")

    # ← ここで exclude_id=id を必ず渡す！
    if has_duplicate_period(type_, start_date, end_date, start_time, end_time, exclude_id=id):
        messages.error(request, "重複している募集期間がすでに存在します。")
        return redirect("shiftgenerator:shift-management-view")

    period.type = type_
    period.start_date = start_date
    period.end_date = end_date
    period.start_time = start_time
    period.end_time = end_time
    period.auto_close_date = auto_close_date
    period.save()
    messages.success(request, "募集期間を更新しました。")
    return redirect('shiftgenerator:shift-management-view')


@require_POST
def shift_period_delete(request, period_id):
    period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)
    period.delete()
    return redirect('shiftgenerator:shift-management-view')

@require_POST
@user_passes_test(lambda u: u.is_superuser)
def shift_period_reopen(request, period_id):
    period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)

    auto_close = request.POST.get("auto_close_date")  # 任意入力
    period.is_active = True

    if auto_close:
        dt = parse_datetime(auto_close)
        if dt and timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        if dt:
            period.auto_close_date = dt
    else:
        # ★入力が無い場合：
        #   期限切れの auto_close_date が残ってると即停止されるのでクリアする
        if period.auto_close_date and period.auto_close_date <= timezone.now():
            period.auto_close_date = None

    period.save()
    messages.success(request, "募集期間を再開しました。")

    return redirect(f"{reverse('shiftgenerator:shift-management-view')}?period_id={period.id}")

@require_POST
@user_passes_test(lambda u: u.is_superuser)
@transaction.atomic
def shift_period_publish(request, period_id):
    period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)

    qs = (
        ShiftPreference.objects
        .filter(
            date__range=(period.start_date, period.end_date),
            staff__is_active=True,
        )
        .prefetch_related('register_assignments', 'published_register_assignments')
    )

    now = timezone.now()

    changed_shift_ids = set()
    regs_rebuild_shift_ids = set()
    bulk_regs = []

    for shift in qs:
        # --- ① 時刻の差分判定 ---
        confirmed_s = shift.confirmed_starttime
        confirmed_e = shift.confirmed_endtime
        published_s = shift.published_starttime
        published_e = shift.published_endtime

        time_changed = (confirmed_s != published_s) or (confirmed_e != published_e)
        if time_changed:
            changed_shift_ids.add(shift.id)

        # --- ② レジの差分判定（time型で比較） ---
        confirmed_regs = set()
        for r in shift.register_assignments.all():
            if r.register_start_time and r.register_end_time:
                confirmed_regs.add((
                    int(r.register_number),
                    r.register_start_time,
                    r.register_end_time,
                ))

        published_regs = set()
        for r in shift.published_register_assignments.all():
            if r.register_start_time and r.register_end_time:
                published_regs.add((
                    int(r.register_number),
                    r.register_start_time,
                    r.register_end_time,
                ))

        regs_changed = (confirmed_regs != published_regs)
        if regs_changed:
            regs_rebuild_shift_ids.add(shift.id)

            # 公開レジを作り直す（確定が無い日は公開レジも空でOK）
            if confirmed_s and confirmed_e:
                for (num, st, et) in confirmed_regs:
                    bulk_regs.append(PublishedRegisterAssignment(
                        shift=shift,
                        register_number=num,
                        register_start_time=st,
                        register_end_time=et,
                    ))

    # --- ③ 時刻スナップショット（差分だけ） ---
    updated_time = 0
    if changed_shift_ids:
        updated_time = (
            ShiftPreference.objects
            .filter(id__in=list(changed_shift_ids))
            .update(
                published_starttime=F('confirmed_starttime'),
                published_endtime=F('confirmed_endtime'),
                published_at=now,
                published_period=period,
            )
        )

    # --- ④ レジスナップショット（差分だけ） ---
    rebuilt_regs = 0
    if regs_rebuild_shift_ids:
        PublishedRegisterAssignment.objects.filter(shift_id__in=list(regs_rebuild_shift_ids)).delete()
        if bulk_regs:
            PublishedRegisterAssignment.objects.bulk_create(bulk_regs)
            rebuilt_regs = len(bulk_regs)

        # レジが変わった＝公開内容が変わった、なので published_at も更新
        ShiftPreference.objects.filter(id__in=list(regs_rebuild_shift_ids)).update(
            published_at=now,
            published_period=period,
        )
    
    # ★差分の有無に関係なく、この期間の「公開対象シフト」は period を必ず付ける
    # これがないと、今回公開した期間に重なってるけど内容に変更がないシフトが、次回以降の公開で漏れてしまう
    ShiftPreference.objects.filter(
        date__range=(period.start_date, period.end_date),
        staff__is_active=True,
        published_starttime__isnull=False,
        published_endtime__isnull=False,
    ).exclude(published_period=period).update(published_period=period)

    if not changed_shift_ids and not regs_rebuild_shift_ids:
        messages.info(request, "変更がないため公開はスキップしました（差分なし）")
    else:
        messages.success(
            request,
            f"公開しました（時刻更新: {updated_time}件 / レジ再作成: {len(regs_rebuild_shift_ids)}件 / 公開レジ行: {rebuilt_regs}）"
        )

    return redirect(f"{reverse('shiftgenerator:shift-management-view')}?period_id={period.id}")

@login_required
def published_shift_events_api(request):
    user = request.user
    staff_profile = getattr(user, 'staff_profile', None)
    if not staff_profile:
        return JsonResponse({'error': 'no staff'}, status=403)

    start = parse_date(request.GET.get('start'))
    end = parse_date(request.GET.get('end'))
    if not start or not end:
        return JsonResponse({'error': 'start/end required'}, status=400)

    qs = (ShiftPreference.objects
          .filter(staff=staff_profile, date__gte=start, date__lt=end)
          .select_related('published_period')
          .prefetch_related('published_register_assignments'))
    
    # periodごとの「最新公開日時（published_at の max）」をまとめて作る
    period_ids = {s.published_period_id for s in qs if s.published_period_id}
    latest_map = {}
    if period_ids:
        rows = (
            ShiftPreference.objects
            .filter(published_period_id__in=period_ids, published_at__isnull=False)
            .values('published_period_id')
            .annotate(latest=Max('published_at'))
        )
        latest_map = {r['published_period_id']: r['latest'] for r in rows}

    events = []
    for shift in qs:
        # 休み表示は「希望と同じロジック」でOK（そのまま見せる）
        if shift.holiday:
            # shift_events_api と同じ色/タイプ付けに合わせるのが安全
            if shift.holiday.id == 1:
                t, color = "off", "#ff3d3d"
            elif shift.holiday.id == 2:
                t, color = "school", "#12a8b3"
            elif shift.holiday.id == 3:
                t, color = "pending", "#ede100"
            else:
                t, color = "off", "#ff3d3d"

            events.append({
                "id": str(shift.id),
                "title": shift.holiday.holiday_name,
                "start": shift.date.isoformat() + "T00:00:00",
                "end": shift.date.isoformat() + "T23:59:59",
                "display": "block",
                "extendedProps": {
                    "type": t,
                    "holiday": True,
                    # PC装飾で使ってるなら合わせる
                    "holidayColor": color,
                }
            })
            continue

        # 公開版（published_*）が入ってる日のみ“確定”として表示
        if shift.published_starttime and shift.published_endtime:
            regs = []
            for r in shift.published_register_assignments.all().order_by('register_start_time', 'register_end_time', 'register_number'):
                if r.register_start_time and r.register_end_time:
                    regs.append({
                        "register_number": r.register_number,
                        "register_start_time": r.register_start_time.strftime("%H:%M"),
                        "register_end_time": r.register_end_time.strftime("%H:%M"),
                    })

            published_at = shift.published_at.isoformat() if shift.published_at else None
            # 「調整中」判定：ドラフト更新が公開より新しい
            is_adjusting = bool(shift.published_at and shift.updated_at and shift.updated_at > shift.published_at)

            p = shift.published_period
            period_payload = None
            if p:
                latest_dt = latest_map.get(p.id)
                period_payload = {
                    "id": p.id,
                    "label": p.label,
                    "start_date": p.start_date.isoformat(),
                    "end_date": p.end_date.isoformat(),
                    "type": p.type,
                    # この募集期間内での最新公開日時（max）
                    "latest_published_at": latest_dt.isoformat() if latest_dt else None,
                }
                
            events.append({
                "id": str(shift.id),
                "title": f'{shift.published_starttime.strftime("%H:%M")} - {shift.published_endtime.strftime("%H:%M")}',
                "start": f'{shift.date}T{shift.published_starttime.strftime("%H:%M:%S")}',
                "end": f'{shift.date}T{shift.published_endtime.strftime("%H:%M:%S")}',
                "extendedProps": {
                    "type": "shift",          # 既存のUI処理に乗せる（スマホの判定もこれ）
                    "holiday": False,
                    "starttime": shift.published_starttime.strftime("%H:%M"),
                    "endtime": shift.published_endtime.strftime("%H:%M"),
                    "registers": regs,
                    "published_at": published_at,
                    "published_period": period_payload,
                    "is_adjusting": is_adjusting,
                }
            })

    return JsonResponse(events, safe=False)

@login_required
def published_period_meta_api(request):
    user = request.user
    staff = getattr(user, 'staff_profile', None)
    if not staff:
        return JsonResponse({'error': 'no staff'}, status=403)

    start = parse_date(request.GET.get('start'))
    end   = parse_date(request.GET.get('end'))
    if not start or not end:
        return JsonResponse({'error': 'start/end required'}, status=400)

    # このレンジに関係する period を取る（公開/非公開どちらも返してOK）
    periods = ShiftSubmissionPeriod.objects.filter(
        start_date__lt=end,
        end_date__gte=start,
        is_active=True,
    ).order_by('start_date')

    # staff × period ごとの「期間の公開日時」を集計
    payload = []
    for p in periods:
        qs = ShiftPreference.objects.filter(
            staff=staff,
            date__gte=p.start_date,
            date__lte=p.end_date,
        )
        # published_at が入っているレコードのMaxを取る
        dt = qs.filter(published_at__isnull=False).aggregate(mx=Max('published_at'))["mx"]
        # 確定シフトが存在するかどうかも返す
        has_confirmed = qs.filter(
            confirmed_starttime__isnull=False,
            confirmed_endtime__isnull=False,
        ).exists()

        payload.append({
            "id": p.id,
            "label": p.label,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat(),
            "type": p.type,
            "period_published_at": dt.isoformat() if dt else None,
            "has_confirmed": has_confirmed,  # ★追加
        })

    return JsonResponse({"periods": payload})

@require_POST
@user_passes_test(lambda u: u.is_superuser)
def shift_period_stop(request, period_id):
    period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)
    period.is_active = False
    period.save()
    messages.success(request, "募集期間を停止しました。")
    return redirect('shiftgenerator:shift-management-view')

@superuser_required
def shift_calendar_view(request):
    # 必要なロジック（たとえば、今までshift_management_viewでやっていたカレンダーロジックなど）
    selected_date_str = request.GET.get('date')
    if selected_date_str:
        try:
            selected_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
        except ValueError:
            selected_date = localdate()
    else:
        today = localdate()
        if today.day <= 15:
            selected_date = date(today.year, today.month, 16)
        else:
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year if today.month < 12 else today.year + 1
            selected_date = date(next_year, next_month, 1)

    preferences = ShiftPreference.objects.filter(
        date=selected_date,
        starttime__isnull=False,
        endtime__isnull=False
    ).select_related('staff')

    # 有効スタッフ（is_active=True）は全員表示
    active_staff = list(Staff.objects.filter(is_active=True))

    # 無効スタッフ（is_active=False）はその日「確定シフト」がある場合だけ表示
    inactive_staff = list(
        Staff.objects.filter(is_active=False).filter(
            Exists(
                ShiftPreference.objects.filter(
                    staff=OuterRef('pk'),
                    date=selected_date,
                    confirmed_starttime__isnull=False,
                    confirmed_endtime__isnull=False
                )
            )
        )
    )

    # Pythonで合体＆ID順ソート
    staff = sorted(active_staff + inactive_staff, key=lambda s: s.id)


    context = {
        'preferences': preferences,
        'staff': staff,
        'selected_date': selected_date
    }
    # テンプレートをカレンダー用に（新ファイルに）
    return render(request, 'shiftgenerator/shift_calendar_view.html', context)

@require_GET
def api_shift_items(request):
    """指定日のシフト(items)・レジ(items)・希望(wish)を返す"""
    date_str = request.GET.get('date')
    target_date = parse_date(date_str) if date_str else timezone.localdate()
    if target_date is None:
        target_date = timezone.localdate()
    
    # ★ 表示対象スタッフ（active + その日に確定シフトがある inactive）
    staff = get_display_staff_for_date(target_date)

    # --- 確定シフト ---
    preferences = ShiftPreference.objects.filter(
        date=target_date,
        confirmed_starttime__isnull=False,
        confirmed_endtime__isnull=False
    )

    # --- 希望シフト ---
    wish_preferences = ShiftPreference.objects.filter(
        date=target_date,
        starttime__isnull=False,
        endtime__isnull=False
    )

    # --- レジ割当（★ここ重要：日付で絞る） ---
    register_assignments = ShiftRegisterAssignment.objects.filter(
        shift__date=target_date
    )

    shift_items = []
    for p in preferences:
        shift_items.append({
            "id": p.id,
            "content": f"{p.confirmed_starttime.strftime('%H:%M')}-{p.confirmed_endtime.strftime('%H:%M')}",
            "start": datetime.combine(p.date, p.confirmed_starttime).isoformat(),
            "end": datetime.combine(p.date, p.confirmed_endtime).isoformat(),
            "group": p.staff.id,
            "type": "range",
            "is_shift": True,
            "is_reg": False,
        })

    reg_items = []
    for reg in register_assignments:
        reg_start = datetime.combine(reg.shift.date, reg.register_start_time)
        reg_end   = datetime.combine(reg.shift.date, reg.register_end_time)
        reg_items.append({
            "id": f"reg-{reg.id}",
            "group": reg.shift.staff.id,
            "content": reg.get_register_display() if hasattr(reg, "get_register_display") else f"レジ{reg.register_number}",
            "start": reg_start.isoformat(),
            "end": reg_end.isoformat(),
            "type": "range",
            "editable": False,
            "className": f"register-item reg{reg.register_number}",
            "shift_id": reg.shift.id,
            "register_number": reg.register_number,
            "is_shift": False,
            "is_reg": True,
        })

    wish_items = []
    for w in wish_preferences:
        wish_items.append({
            "id": f"wish-{w.id}",
            "content": "",
            "start": datetime.combine(w.date, w.starttime).isoformat(),
            "end": datetime.combine(w.date, w.endtime).isoformat(),
            "group": w.staff.id,
            "type": "background",
            "className": "wish-item",
            "is_shift": False,
            "is_reg": False,
        })

    return JsonResponse({
        "date": target_date.strftime("%Y-%m-%d"),
        "items": shift_items + reg_items + wish_items,
        "groups": [{"id": s.id, "name": s.name} for s in staff],
    })


@superuser_required
def shift_management(request):
    date_str = request.GET.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        today = timezone.localdate()
        if today.day <= 15:
            target_date = date(today.year, today.month, 16)
        else:
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year if today.month < 12 else today.year + 1
            target_date = date(next_year, next_month, 1)
    
    # period一覧（とりあえず active を並べる）
    periods = ShiftSubmissionPeriod.objects.all().order_by('-start_date', '-id')

    # ★追加：選択中period（URL ?period_id=xx か、なければ「今日を含むactive」→なければ先頭）
    pid = request.GET.get('period_id')
    selected_period_id = None
    if pid and pid.isdigit():
        selected_period_id = int(pid)
    else:
        today = localdate()
        p = periods.filter(start_date__lte=today, end_date__gte=today).first()
        if p:
            selected_period_id = p.id
        else:
            selected_period_id = periods.first().id if periods.exists() else None

    # --- 確定シフト（編集対象） ---
    preferences = ShiftPreference.objects.filter(
        date=target_date,
        confirmed_starttime__isnull=False,
        confirmed_endtime__isnull=False
    )

    # --- 希望シフト（original/wish） ---
    wish_preferences = ShiftPreference.objects.filter(
        date=target_date,
        starttime__isnull=False,
        endtime__isnull=False,
        # confirmed_starttime=None でもOK（未確定のみ出したい場合）
    )

    staff = get_display_staff_for_date(target_date)


    for preference in preferences:
        preference.confirmed_starttime = datetime.combine(preference.date, preference.confirmed_starttime)
        preference.confirmed_endtime = datetime.combine(preference.date, preference.confirmed_endtime)

    register_assignments = ShiftRegisterAssignment.objects.filter(
        shift__date=target_date
    )
    register_data = []
    for reg in register_assignments:
        # reg.register_start_time と reg.register_end_time は TimeField
        reg_start = datetime.combine(reg.shift.date, reg.register_start_time)
        reg_end = datetime.combine(reg.shift.date, reg.register_end_time)

        register_data.append({
            'id': reg.id,
            'shift_id': reg.shift.id,
            'group': reg.shift.staff.id,
            'content': f'レジ{reg.register_number}',
            'start': reg_start.isoformat(),
            'end': reg_end.isoformat(),
            'className': f'register-item reg{reg.register_number}',
            'register_number': reg.register_number, 
        })
        
    context = {
        'preferences': preferences,
        'staff': staff,
        'wish_preferences': wish_preferences,
        'date': target_date.isoformat(),
        'selected_date': target_date,
        'register_assignments': register_data,
        'periods': periods,
        'selected_period_id': selected_period_id,
    }
    return render(request, 'shiftgenerator/shift_management.html', context)

# views.py
@login_required
@require_POST
@user_passes_test(lambda u: u.is_superuser)
def bulk_reset_preview(request):
    import json
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "JSON が不正です。"}, status=400)

    period_id = payload.get("period_id")
    if not period_id:
        return JsonResponse({"success": False, "error": "period_id が必要です。"}, status=400)

    period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)

    start = parse_date(payload.get("start")) if payload.get("start") else period.start_date
    end   = parse_date(payload.get("end"))   if payload.get("end")   else period.end_date
    if not start or not end or start > end:
        return JsonResponse({"success": False, "error": "日付範囲が不正です。"}, status=400)
    
    if start < period.start_date or end > period.end_date:
        return JsonResponse({
            "success": False,
            "error": f"募集期間内を指定してください。（募集期間: {period.start_date}〜{period.end_date}）"
        }, status=400)

    # period内に丸める
    if start < period.start_date: start = period.start_date
    if end > period.end_date: end = period.end_date

    staff_id = payload.get("staff_id")  # null or int

    staff_qs = Staff.objects.filter(is_active=True)
    if staff_id is not None:
        staff_qs = staff_qs.filter(id=staff_id)
    if not staff_qs.exists():
        return JsonResponse({"success": False, "error": "対象スタッフが見つかりません。"}, status=404)

    submissions = ShiftSubmission.objects.filter(period=period, staff__in=staff_qs)
    sub_cnt = submissions.count()
    
    # ① 削除対象の確定シフト数（DBから正確に）
    confirmed_qs = ShiftPreference.objects.filter(
        staff__in=staff_qs,
        date__gte=start, date__lte=end,
        confirmed_starttime__isnull=False,
        confirmed_endtime__isnull=False,
    )
    shifts = confirmed_qs.count()

    # ② 削除対象のレジ割当数
    regs = ShiftRegisterAssignment.objects.filter(shift__in=confirmed_qs).count()

    # ③ 追加される確定シフト数（提出 snapshot から数える）
    wishes = 0
    holiday_rows = 0
    time_rows = 0

    for sub in submissions:
        snap = sub.snapshot or []
        for row in snap:
            d = parse_date(row.get("date")) if row.get("date") else None
            if not d or d < start or d > end:
                continue
            # holidayは確定にはコピーしない前提（= wishesに数えない）
            if row.get("holiday_id"):
                holiday_rows += 1
                continue
            
            if row.get("starttime") and row.get("endtime"):
                time_rows += 1
                wishes += 1

    return JsonResponse({"success": True,
                         "shifts": shifts,
                         "regs": regs,
                         "wishes": wishes,
                         "sub_cnt": sub_cnt,
                         "holiday_rows": holiday_rows,
                         "time_rows": time_rows,})

def parse_time_any(s: str):
    # "14:00" / "14:00:00" 両対応
    return time.fromisoformat(s)

def get_dow_for_date(d):
    return DayOfWeek.objects.get(day_number=d.weekday())  # 月0..日6

@login_required
@require_POST
@transaction.atomic
def bulk_reset_to_wish(request):
    """
    指定した募集期間(period_id)の範囲で
      - いったん確定シフト＋レジを全部クリア
      - そのあと「提出時 snapshot」から確定(confirmed_*)を作り直す

    staff_id があればその人だけ、無ければ全員（is_active=True）対象。
    start/end が渡された場合は period 範囲内に丸める（渡されなければ period 全体）。
    """
    import json
    from datetime import timedelta, datetime, time
    from django.db import transaction

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "JSON が不正です。"}, status=400)

    period_id = payload.get("period_id")
    if not period_id:
        return JsonResponse({"success": False, "error": "period_id が必要です。"}, status=400)

    period = get_object_or_404(ShiftSubmissionPeriod, id=period_id)

    # 反映範囲：payload があれば period内に丸める、無ければ period 全体
    start = parse_date(payload.get("start")) if payload.get("start") else period.start_date
    end   = parse_date(payload.get("end"))   if payload.get("end")   else period.end_date

    if not start or not end or start > end:
        return JsonResponse({"success": False, "error": "日付範囲が不正です。"}, status=400)
    
    if start < period.start_date or end > period.end_date:
        return JsonResponse({
            "success": False,
            "error": f"募集期間内を指定してください。（募集期間: {period.start_date}〜{period.end_date}）"
        }, status=400)

    if start < period.start_date:
        start = period.start_date
    if end > period.end_date:
        end = period.end_date
    
    staff_id = payload.get("staff_id")

    staff_qs = Staff.objects.filter(is_active=True)
    if staff_id is not None:
        staff_qs = staff_qs.filter(id=staff_id)

    if not staff_qs.exists():
        return JsonResponse({"success": False, "error": "対象スタッフが見つかりません。"}, status=404)

    warnings = []
    total_reset = 0

    # snapshot を staffごとに先に取っておく（N+1を減らす）
    submissions = {
        s.staff_id: s
        for s in ShiftSubmission.objects.filter(period=period, staff__in=staff_qs)
    }

    # 期間内の確定を全部クリア（スタッフ単位でまとめてやる）
    with transaction.atomic():
        # 対象期間の ShiftPreference（対象スタッフ）
        base_qs = ShiftPreference.objects.filter(
            staff__in=staff_qs,
            date__gte=start,
            date__lte=end,
        )

        # ① 確定シフトに紐づくレジを全部削除
        confirmed_qs = base_qs.filter(
            confirmed_starttime__isnull=False,
            confirmed_endtime__isnull=False,
        )
        shift_ids = list(confirmed_qs.values_list("id", flat=True))
        if shift_ids:
            ShiftRegisterAssignment.objects.filter(shift_id__in=shift_ids).delete()

        # ② confirmed を全部クリア
        confirmed_qs.update(confirmed_starttime=None, confirmed_endtime=None)

        # ③ “confirmed-only で作ったゴミ行” を先に掃除（空行を消す）
        #    ※希望(start/end)もholidayもなく、confirmedも空になった行が残るので削除
        base_qs.filter(
            starttime__isnull=True,
            endtime__isnull=True,
            holiday__isnull=True,
            confirmed_starttime__isnull=True,
            confirmed_endtime__isnull=True,
            published_starttime__isnull=True,
            published_endtime__isnull=True,
            published_period__isnull=True,
        ).delete()

        # ④ staffごとに snapshot から確定を復元
        for staff in staff_qs:
            sub = submissions.get(staff.id)
            if not sub or not sub.snapshot:
                warnings.append(f"{staff.name}：提出が無い（snapshot無し）ためスキップ")
                continue

            # snapshot から、対象範囲内の「シフト（holidayなし）」だけ抽出
            items = []
            for row in sub.snapshot:
                d_str = row.get("date")
                st = row.get("starttime")
                et = row.get("endtime")
                holiday_id = row.get("holiday_id")

                if not d_str:
                    continue
                d = parse_date(d_str)
                if not d or d < start or d > end:
                    continue

                # holiday は確定にはコピーしない（確定削除＝休みは表示ロジックで表現）
                if holiday_id:
                    continue

                if st and et:
                    items.append((d, st, et))

            if not items:
                warnings.append(f"{staff.name}：期間内に提出シフトが無い（holidayのみ/空）")
                continue

            # 同日2件まで想定：既存の希望行に一致があればそこにconfirmedを付与、
            # 無ければ confirmed-only 行を新規作成して復元
            for (d, st_str, et_str) in items:
                # "HH:MM" -> time
                st_time = time.fromisoformat(st_str)
                et_time = time.fromisoformat(et_str)

                # まず同じ希望行（start/end一致）があればそれを使う
                target = ShiftPreference.objects.filter(
                    staff=staff,
                    date=d,
                    starttime=st_time,
                    endtime=et_time,
                ).first()
                
                JP = ["月", "火", "水", "木", "金", "土", "日"]

                dow, _ = DayOfWeek.objects.get_or_create(
                    day_number=d.weekday(),
                    defaults={"day_name": JP[d.weekday()]}
                )

                if target:
                    target.confirmed_starttime = st_time
                    target.confirmed_endtime = et_time
                    target.save(update_fields=["confirmed_starttime", "confirmed_endtime"])
                else:
                    # 希望が消されてても復元できるよう confirmed-only 行を作る
                    ShiftPreference.objects.create(
                        staff=staff,
                        date=d,
                        day_of_week=dow,
                        starttime=None,
                        endtime=None,
                        holiday=None,
                        confirmed_starttime=st_time,
                        confirmed_endtime=et_time,
                    )

                total_reset += 1

    return JsonResponse({
        "success": True,
        "count": total_reset,
        "warnings": warnings,
        "period": {
            "id": period.id,
            "label": period.label,
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "type": period.type,
        }
    })

# shift_formのためのどこ見てるかフック
@login_required
def shift_events_api(request):
    user = request.user
    staff_profile = getattr(user, 'staff_profile', None)
    if not staff_profile:
        return JsonResponse({'error': 'no staff'}, status=403)

    start = parse_date(request.GET.get('start'))
    end = parse_date(request.GET.get('end'))
    if not start or not end:
        return JsonResponse({'error': 'start/end required'}, status=400)

    qs = ShiftPreference.objects.filter(
        staff=staff_profile,
        date__gte=start,
        date__lt=end,  # endはexclusiveにすると扱いやすい
    )

    events = []
    for shift in qs:
        if shift.holiday:
            if shift.holiday.id == 1:
                t = "off"
                color = "#ff3d3d"
            elif shift.holiday.id == 2:
                t = "school"
                color = "#12a8b3"   # 青□にしたいなら青。水色帯にしたいなら "#12a8b3"
            elif shift.holiday.id == 3:
                t = "pending"
                color = "#ede100"
            else:
                t = "off"
                color = "#ff3d3d"

            events.append({
                "id": str(shift.id),  # ★ついでに文字列統一（重複事故防止）
                "title": shift.holiday.holiday_name,
                "start": shift.date.isoformat() + "T00:00:00",
                "end": shift.date.isoformat() + "T23:59:59",
                "display": "block",
                "extendedProps": {
                    "type": t,
                    "holiday": True,
                    "holidayColor": color,
                }
            })
        elif shift.starttime and shift.endtime:
            events.append({
                "id": shift.id,
                "title": f'{shift.starttime.strftime("%H:%M")} - {shift.endtime.strftime("%H:%M")}',
                "start": f'{shift.date}T{shift.starttime.strftime("%H:%M:%S")}',
                "end": f'{shift.date}T{shift.endtime.strftime("%H:%M:%S")}',
                "extendedProps": {
                    "type": "shift",
                    "holiday": False,
                    "starttime": shift.starttime.strftime("%H:%M"),
                    "endtime": shift.endtime.strftime("%H:%M"),
                }
            })

    return JsonResponse(events, safe=False)

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
    
    # 受付中の期間リスト取得
    active_periods = ShiftSubmissionPeriod.objects.filter(is_active=True).order_by('start_date')

    # 日付の初期位置
    today = localdate()
    # 受付中の募集期間（デフォルトだけを優先）
    default_active_qs = ShiftSubmissionPeriod.objects.filter(
        is_active=True,
        type="default",
    ).order_by("created_at", "id")  # created_at が同値でも安定するように id も

    default_active = default_active_qs.first()

    if default_active:
        initial_date = default_active.start_date
    else:
        # 半月ロジック
        if today.day <= 15:
            initial_date = date(today.year, today.month, 16)
        else:
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year if today.month < 12 else today.year + 1
            initial_date = date(next_year, next_month, 1)
    # データをJSON形式に変換
    events = []
    
    # シフトデータを元にイベントを生成
    for shift in shifts:
        if shift.holiday:
            name = shift.holiday.holiday_name  # 例: "休み" / "学校関連" / "未定"

            # 休みの種類に応じて色
            if shift.holiday.id == 1:
                holiday_color = '#ff3d3d'
            elif shift.holiday.id == 2:
                holiday_color = '#12a8b3'
            elif shift.holiday.id == 3:
                holiday_color = '#ede100'
            else:
                holiday_color = 'red'

            # ✅ type 判定（ここが重要）
            if "学校" in name:
                t = "school"
            elif "未定" in name:
                t = "pending"
            else:
                t = "off"   # それ以外は「休み」

            events.append({
                'title': name,
                'start': shift.date.isoformat() + 'T00:00:00',
                'end': shift.date.isoformat() + 'T23:59:59',
                'id': shift.id,
                'display': 'block',
                'extendedProps': {
                    'holiday': True,
                    'holidayColor': holiday_color,
                    'starttime': None,
                    'endtime': None,
                    'type': t,              # ✅ ここで返す
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
                'backgroundColor': 'blue',
                'extendedProps': {
                    'holiday': False,
                    'starttime': shift.starttime.strftime("%H:%M"),
                    'endtime': shift.endtime.strftime("%H:%M"),
                    'type': "shift",        # ✅ 一応これも入れとくと安定
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
        'active_periods': active_periods,
        'events': events_json,
        'name': staff_profile.name,
        'username': user.username,
        'history': history_json if history_list else None,
        'initial_date': initial_date.isoformat(),
    }
    
    return render(request, 'shiftgenerator/shift_form.html', context)

@login_required
def submit_shift(request):
    user = request.user
    staff_profile = getattr(user, 'staff_profile', None)
    if not staff_profile:
        return JsonResponse({'success': False, 'error': 'スタッフ情報が見つかりません。'})

    if request.method == "POST":
        period_id = request.POST.get("period_id")
        if not period_id:
            return JsonResponse({'success': False, 'error': '提出期間を選択してください。'})
        period = get_object_or_404(ShiftSubmissionPeriod, id=period_id, is_active=True)

        # シフト希望一覧をdictリスト化、日付・時間はすべてisoformat文字列に変換
        shift_qs = ShiftPreference.objects.filter(
            staff=staff_profile,
            date__range=(period.start_date, period.end_date)
        ).filter(
            Q(holiday__isnull=False) | (Q(starttime__isnull=False) & Q(endtime__isnull=False))
        ).order_by('date', 'starttime', 'endtime', 'holiday_id')
                
        from collections import defaultdict

        def _validate_shift_prefs_for_submit(shift_qs):
            errors = []
            by_date = defaultdict(list)

            for pref in shift_qs:
                by_date[pref.date].append(pref)

            for d, items in sorted(by_date.items(), key=lambda x: x[0]):
                holidays = [x for x in items if x.holiday_id is not None]
                normals = [x for x in items if x.holiday_id is None]  # ← この時点で start/end は必ず両方ある想定

                d_str = d.strftime('%Y-%m-%d')

                if len(holidays) >= 2:
                    errors.append(f"{d_str}：休みが複数登録されています（{len(holidays)}件）")

                if holidays and normals:
                    errors.append(f"{d_str}：休みと予定が同日に登録されています（どちらか削除してください）")

                if len(normals) > 2:
                    errors.append(f"{d_str}：予定が{len(normals)}件あります（最大2件まで）")

                normals_sorted = sorted(normals, key=lambda x: (x.starttime, x.endtime))

                # 時刻逆転（最終防衛）
                for x in normals_sorted:
                    if x.endtime <= x.starttime:
                        errors.append(f"{d_str}：開始/終了時刻が不正です（{x.starttime.strftime('%H:%M')}〜{x.endtime.strftime('%H:%M')}）")

                # 重なりチェック（1回だけ）
                prev = None
                for x in normals_sorted:
                    if prev and prev.endtime > x.starttime:
                        errors.append(
                            f"{d_str}：予定が重なっています（{prev.starttime.strftime('%H:%M')}〜{prev.endtime.strftime('%H:%M')} と "
                            f"{x.starttime.strftime('%H:%M')}〜{x.endtime.strftime('%H:%M')}）"
                        )
                    prev = x

            return errors


        # ★締切チェック（念のため）
        if period.auto_close_date and timezone.now() > period.auto_close_date:
            return JsonResponse({'success': False, 'error': '提出期限を過ぎています。'}, status=200)

        # ★提出時の最終整合チェック（詳細つき）
        validation_errors = _validate_shift_prefs_for_submit(shift_qs)
        if validation_errors:
            return JsonResponse({
                'success': False,
                'error': '提出内容に不備があります。下記を修正してください。',
                'validation_errors': validation_errors
            }, status=200)

        current_prefs = []
        for pref in shift_qs:
            current_prefs.append({
                'date': pref.date.isoformat() if pref.date else None,
                'starttime': pref.starttime.strftime('%H:%M') if pref.starttime else None,
                'endtime': pref.endtime.strftime('%H:%M') if pref.endtime else None,
                'confirmed_starttime': pref.confirmed_starttime.strftime('%H:%M') if pref.confirmed_starttime else None,
                'confirmed_endtime': pref.confirmed_endtime.strftime('%H:%M') if pref.confirmed_endtime else None,
                'holiday_id': pref.holiday_id,
            })

        prev_submission = ShiftSubmission.objects.filter(staff=staff_profile, period=period).first()
        
        if not shift_qs.exists():
            return JsonResponse({
                'success': False,
                'error': '提出期間内に希望が1件もありません。'
            }, status=200)

        if prev_submission and prev_submission.snapshot:
            if current_prefs == prev_submission.snapshot:
                # 完全一致なら「変更なし」
                return JsonResponse({
                    'success': False,
                    'no_change': True,
                    'message': '前回提出内容と全く同じです。'
                })

        comment = request.POST.get("period_comment", "")

        if prev_submission:
            # 既存がある → 更新（再提出）
            prev_submission.comment = comment
            prev_submission.submission_status = "再提出"
            prev_submission.snapshot = current_prefs
            prev_submission.save(update_fields=["comment", "submission_status", "snapshot", "updated_at"])
            status = "再提出"
        else:
            # 既存がない → 作成（初回）
            ShiftSubmission.objects.create(
                staff=staff_profile,
                period=period,
                comment=comment,
                submission_status="初回",
                snapshot=current_prefs
            )
            status = "初回"

        return JsonResponse({
            'success': True,
            'period_label': period.label,
            'status': status
        })

    return JsonResponse({'success': False, 'error': '無効なリクエストです。'})


@login_required
def shift_detail(request, shift_id):
    shift = get_object_or_404(ShiftPreference, id=shift_id, staff=request.user.staff_profile)

    readonly = request.GET.get('readonly') == '1'
    
    # ▼ 追加：初期化（readonlyじゃなくても未定義にならないように）
    period_label = None
    period_start = None
    period_end = None
    period_latest_published_at = None

    # readonly中は削除させない（URL直叩き対策）
    if readonly and request.method == 'POST':
        return HttpResponseNotAllowed(['GET'])

    # ▼ 表示する時間：readonlyなら公開版
    if readonly:
        shift_starttime = shift.published_starttime.strftime("%H:%M") if shift.published_starttime else '--'
        shift_endtime   = shift.published_endtime.strftime("%H:%M") if shift.published_endtime else '--'
    else:
        shift_starttime = shift.starttime.strftime("%H:%M") if shift.starttime else '--'
        shift_endtime   = shift.endtime.strftime("%H:%M") if shift.endtime else '--'
    holiday_name    = shift.holiday.holiday_name if shift.holiday else '--'

    registers = []
    if readonly:
        PublishedRegisterAssignment.objects
        registers = (
            PublishedRegisterAssignment.objects
            .filter(shift=shift)
            .order_by('register_number', 'register_start_time')
        )
        
        # ▼▼ 追加：募集期間の最新公開日時（period内のpublished_atのmax） ▼▼
        period_label = None
        period_start = None
        period_end = None
        period_latest_published_at = None

        p = shift.published_period
        if p:
            period_label = p.label
            period_start = p.start_date
            period_end = p.end_date

            # 同じ募集期間の公開済みの中で最新（このスタッフ分）
            period_latest_published_at = (ShiftPreference.objects
                .filter(published_period=p, published_at__isnull=False)
                .aggregate(m=Max('published_at'))
                .get('m')
            )

            # 表示をJSTに寄せたい場合（USE_TZ=True前提）
            if period_latest_published_at:
                period_latest_published_at = timezone.localtime(period_latest_published_at)

    if request.method == 'POST':
        # ✅ ここが重要：行削除ではなく「希望だけ削除」にする
        has_confirmed = bool(shift.confirmed_starttime and shift.confirmed_endtime)
        has_published = bool(
            (shift.published_starttime and shift.published_endtime) or shift.published_at
        )

        if has_confirmed or has_published:
            # 希望だけクリア（確定/公開は残す）
            shift.starttime = None
            shift.endtime = None
            shift.holiday = None
            shift.save(update_fields=['starttime', 'endtime', 'holiday', 'updated_at'])
            messages.success(request, '希望シフトだけ削除しました（確定/公開は保持）')
        else:
            # 希望しかないレコードなら削除してもOK（運用でNULL化に統一でもOK）
            shift.delete()
            messages.success(request, '希望シフトを削除しました。')

        return redirect('shiftgenerator:shift-form')

    return render(request, 'shiftgenerator/shift_detail.html', {
        'shift': shift,
        'shift_starttime': shift_starttime,
        'shift_endtime': shift_endtime,
        'holiday_name': holiday_name,
        'readonly': readonly,
        'registers': registers,
        'period_label': period_label,
        'period_start': period_start,
        'period_end': period_end,
        'period_latest_published_at': period_latest_published_at,
    })

def touch_shift_history(staff_profile, starttime, endtime, limit=10):
    """
    (starttime, endtime) を「最近使った」にする。
    既存なら updated_at を今に更新、なければ作成。
    ついでに limit 超えたら「一番古い(=使われてない)」のを削除。
    """
    obj, created = ShiftHistory.objects.get_or_create(
        staff=staff_profile,
        starttime=starttime,
        endtime=endtime,
    )

    if not created:
        # auto_now は save() の時に更新されるけど、
        # 値が変わらないと save しない実装になりがちなので明示 update
        ShiftHistory.objects.filter(pk=obj.pk).update(updated_at=timezone.now())

    # limit 超えたら「updated_at が古い」ものから消す
    qs = ShiftHistory.objects.filter(staff=staff_profile).order_by('updated_at', 'created_at')
    if qs.count() > limit:
        qs.first().delete()
        
# シフト履歴を取得するAPI
def get_shift_history(request):
    staff_profile = request.user.staff_profile

    histories = (
        ShiftHistory.objects
        .filter(staff=staff_profile)
        .order_by('-updated_at', '-created_at')[:10]
    )

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
                if user.is_superuser:
                    return redirect('shiftgenerator:admin-home')
                else:
                    return redirect('shiftgenerator:staff-home')
            else:
                messages.error(request, 'ユーザー名またはパスワードが無効です。')
        else:
            messages.error(request, 'ログイン中にエラーが発生しました。フォームを確認してください。')
    else:
        form = AuthenticationForm()
    return render(request, 'registration/login.html', {'form': form})



def login_manage(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                auth_login(request, user)
                messages.success(request, 'ログインに成功しました。')
                return redirect('shiftgenerator:shift-management-view')
            else:
                messages.error(request, 'ユーザー名またはパスワードが無効です。')
        else:
            messages.error(request, 'ログイン中にエラーが発生しました。フォームを確認してください。')
    else:
        form = AuthenticationForm()
    return render(request, 'registration/login_manage.html', {'form': form})


@login_required
@user_passes_test(lambda u: u.is_superuser)
def admin_home(request):
    return render(request, 'shiftgenerator/admin_home.html')

@login_required
def staff_home(request):
    # 管理者でアクセスされた場合はadmin_homeへリダイレクトしてもOK
    if request.user.is_superuser:
        return redirect('shiftgenerator:admin-home')
    return render(request, 'shiftgenerator/staff_home.html')

# ログ設定
logger = logging.getLogger(__name__)



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
    # time_slots を作る共通処理（GETと同じ）
    def build_context(date_str):
        user = request.user
        staff_profile = getattr(user, 'staff_profile', None)
        staff_name = staff_profile.name if staff_profile else ''
        time_slots = []
        base_time = datetime(2024, 1, 1, 8, 30)
        end_time = datetime(2024, 1, 1, 20, 0)
        while base_time <= end_time:
            time_slots.append(base_time.strftime('%H:%M'))
            base_time += timedelta(minutes=15)

        return {
            'date': date_str,
            'time_slots': time_slots,
            'staff_name': staff_name,
            'username': user.username
        }

    if request.method != 'POST':
        return redirect('shiftgenerator:shift-form')

    try:
        # JSONリクエストとPOSTリクエストの区別
        is_json = (request.content_type == 'application/json')
        if is_json:
            data = json.loads(request.body)
            date_str = data.get('date')
            time_range = data.get('time_range')
        else:
            date_str = request.POST.get('date')
            time_range = f"{request.POST.get('start_time')} - {request.POST.get('end_time')}"

        start_s, end_s = time_range.split(' - ')
        starttime = datetime.strptime(start_s, '%H:%M').time()
        endtime = datetime.strptime(end_s, '%H:%M').time()

        # ユーザーのスタッフプロファイルを取得
        staff_profile = getattr(request.user, 'staff_profile', None)
        if not staff_profile:
            return redirect('shiftgenerator:index')

        # === ここにあなたが入れたサーバ側バリデーション（例） ===
        if endtime <= starttime:
            msg = '終了時間は開始時間より後にしてください。'
            if is_json:
                return JsonResponse({'success': False, 'error': msg}, status=400)
            messages.error(request, msg)
            return render(request, 'shiftgenerator/shift_register.html', build_context(date_str))

        # 休みが入っている日は予定追加禁止（例）
        if ShiftPreference.objects.filter(staff=staff_profile, date=date_str, holiday__isnull=False).exists():
            msg = '休みが入っている日は、予定を追加できません。'
            if is_json:
                return JsonResponse({'success': False, 'error': msg}, status=400)
            messages.error(request, msg)
            return render(request, 'shiftgenerator/shift_register.html', build_context(date_str))

        # 1日2件まで（例）
        if ShiftPreference.objects.filter(staff=staff_profile, date=date_str, holiday__isnull=True).count() >= 2:
            msg = '1日に登録できる予定は最大2つまでです。'
            if is_json:
                return JsonResponse({'success': False, 'error': msg}, status=400)
            messages.error(request, msg)
            return render(request, 'shiftgenerator/shift_register.html', build_context(date_str))
        # =====================================================

        # 日付と曜日
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        dow = date_obj.weekday()
        day_of_week_instance = DayOfWeek.objects.get(day_number=dow)

        # 保存
        shift = ShiftPreference.objects.create(
            staff=staff_profile,
            starttime=starttime,
            endtime=endtime,
            date=date_str,
            day_of_week=day_of_week_instance
        )

        # ShiftHistoryの更新（ついでに）
        touch_shift_history(staff_profile, starttime, endtime)

        if is_json:
            return JsonResponse({'success': True})
        return redirect('shiftgenerator:shift-form')

    except Exception as e:
        # 予期せぬエラーも「同じ画面に戻して表示」
        is_json = (request.content_type == 'application/json')
        if is_json:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

        messages.error(request, f'登録に失敗しました: {e}')
        date_str = request.POST.get('date') or ''
        return render(request, 'shiftgenerator/shift_register.html', build_context(date_str))


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

        # === 追加: その日に何か入ってたら休み登録禁止 ===
        exists_any = ShiftPreference.objects.filter(
            staff=staff_profile,
            date=date
        ).exists()
        if exists_any:
            return JsonResponse({'success': False, 'error': 'この日は既に予定が入っているため休みを登録できません'}, status=400)

        # Holiday モデルから選択された休みを取得
        try:
            holiday_instance = Holiday.objects.get(id=holiday_id)
        except Holiday.DoesNotExist:
            return JsonResponse({'success': False, 'error': '選択された休みが見つかりませんでした'}, status=400)

        

        # シフト希望を登録（休み）
        new_shift = ShiftPreference.objects.create(
            staff=staff_profile,
            date=date,
            holiday=holiday_instance,  # 休みの種類を登録
            day_of_week=day_of_week_instance  # 曜日情報を登録
        )

        # 登録したシフトの更新時刻を取得
        last_update = new_shift.updated_at.isoformat()  # updated_atをISOフォーマットで取得

        return JsonResponse({'success': True, 'last_update': last_update})# last_updateをレスポンスに含める
    except Exception as e:
        print(f"Error: {str(e)}")  # エラーメッセージをコンソールに出力
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
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

            # === 追加: 時間バリデーション ===
            if endtime <= starttime:
                return JsonResponse({'success': False, 'error': '終了時間は開始時間より後にしてください'}, status=400)

            # === 追加: 休みが入ってる日は予定追加禁止 ===
            has_holiday = ShiftPreference.objects.filter(
                staff=staff_profile,
                date=date,
                holiday__isnull=False
            ).exists()
            if has_holiday:
                return JsonResponse({'success': False, 'error': '休みが入っている日は予定を追加できません'}, status=400)

            # === 追加: 1日2件まで ===
            shift_count = ShiftPreference.objects.filter(
                staff=staff_profile,
                date=date,
                holiday__isnull=True
            ).count()
            if shift_count >= 2:
                return JsonResponse({'success': False, 'error': '1日に登録できる予定は最大2つまでです'}, status=400)

            # シフトの登録
            new_shift = ShiftPreference.objects.create(
                staff=staff_profile,
                starttime=starttime,
                endtime=endtime,
                date=date,
                day_of_week=day_of_week_instance
            )
            touch_shift_history(staff_profile, starttime, endtime)
            
             # 登録したシフトの更新時刻を取得
            last_update = new_shift.updated_at.isoformat()  # updated_atをISOフォーマットで取得

            return JsonResponse({'success': True, 'last_update': last_update})  # last_updateをレスポンスに含める
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})


def get_update_events(request):
    user = request.user
    staff_profile = getattr(user, 'staff_profile', None)

    if not staff_profile:
        return JsonResponse([], safe=False)

    # リクエストパラメータから last_update を取得
    last_update = request.GET.get('last_update')
    
    # last_updateがISOフォーマットなら、parse_datetimeで解析
    last_update_datetime = parse_datetime(last_update) if last_update else None

    if last_update_datetime:
        # last_update 以降に更新された、そのユーザーに関連するシフトのみを取得
        shifts = ShiftPreference.objects.filter(staff=staff_profile, updated_at__gte=last_update_datetime)
        print("更新した奴だけあげるわ")
    else:
        # そのユーザーに関連する全てのシフトを取得
        shifts = ShiftPreference.objects.filter(staff=staff_profile)
        print("全部取るわ！")
    


    events = []
    for shift in shifts:
        if shift.holiday:
            if shift.holiday.id == 1:
                holiday_color = '#ff3d3d'
                marker_type = 'off'
            elif shift.holiday.id == 2:
                holiday_color = '#12a8b3'
                marker_type = 'school'
            elif shift.holiday.id == 3:
                holiday_color = '#ede100'
                marker_type = 'pending'
            else:
                holiday_color = 'red'
                marker_type = 'off'

            events.append({
                'title': shift.holiday.holiday_name,
                'start': shift.date.isoformat() + 'T00:00:00',
                'end': shift.date.isoformat() + 'T23:59:59',
                'id': str(shift.id),  # ★ 文字列に統一（FCの重複回避）
                'display': 'block',
                'extendedProps': {
                    'holiday': True,
                    'holidayColor': holiday_color,
                    'starttime': None,
                    'endtime': None,
                    'type': marker_type,           # ★ここ重要
                }
            })
        elif shift.starttime and shift.endtime:
            events.append({
                'title': f'{shift.starttime.strftime("%H:%M")} - {shift.endtime.strftime("%H:%M")}',
                'id': str(shift.id),  # ★文字列
                'start': f'{shift.date}T{shift.starttime.strftime("%H:%M:%S")}',
                'end': f'{shift.date}T{shift.endtime.strftime("%H:%M:%S")}',
                'backgroundColor': 'blue',
                'extendedProps': {
                    'holiday': False,
                    'starttime': shift.starttime.strftime("%H:%M"),
                    'endtime': shift.endtime.strftime("%H:%M"),
                    'type': 'shift',   # ★
                }
            })

    # print(events)


    return JsonResponse({'success': True, 'events': events}, safe=False)


CIRCLED = {1: "①", 2: "②", 3: "③", 4: "④"}


def _tostr_hm(t):
    """Time -> '8:30' / '20'（:00は省略）"""
    if t is None:
        return ''
    h, m = t.hour, t.minute
    if m == 0:
        return str(h)
    return f"{h}:{m:02d}"


def _fmt_shift_range(start_t, end_t):
    """'8:30~20'"""
    if not start_t or not end_t:
        return ''
    return f"{_tostr_hm(start_t)}~{_tostr_hm(end_t)}"


def _fmt_regs(regs):
    """
    regs: ShiftRegisterAssignment list
    -> '②9~14\n③14~17'
    """
    lines = []
    for r in sorted(regs, key=lambda x: (x.register_start_time or dt_date.min, x.register_number)):
        mark = CIRCLED.get(r.register_number, f"({r.register_number})")
        s = _tostr_hm(r.register_start_time)
        e = _tostr_hm(r.register_end_time)
        if s and e:
            lines.append(f"{mark}{s}~{e}")
    return "\n".join(lines)


def _get_latest_template_path():
    """
    最新アップロードのテンプレを使う。
    ない場合はプロジェクト内のデフォルトを使う想定にしておく。
    """
    latest = ExcelExportTemplate.objects.order_by('-uploaded_at').first()
    if latest and latest.file:
        return latest.file.path

    # デフォルトテンプレ（必要ならパス調整）
    # 例: BASE_DIR / 'shiftgenerator' / 'assets' / 'shift_template.xlsx'
    return getattr(settings, 'EXCEL_EXPORT_DEFAULT_TEMPLATE', None)

def _is_code_cell(v):
    return isinstance(v, str) and re.fullmatch(r"[a-z]{1,3}", v.strip())

def _is_day_cell(v):
    return isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit())

def _find_header_rows(ws, min_codes=6):
    """
    a,b,c... が同じ行に複数並んでいる行を「ヘッダ行」として検出
    min_codes は適当に 6〜10 くらいでOK（あなたのテンプレは余裕で超えるはず）
    """
    headers = []
    for r in range(1, ws.max_row + 1):
        cnt = 0
        for c in range(1, ws.max_column + 1):
            if _is_code_cell(ws.cell(r, c).value):
                cnt += 1
        if cnt >= min_codes:
            headers.append(r)
    return headers

def _build_blocks(ws):
    """
    縦に並ぶ「ページ」をブロックとして抽出する
    block = {header_row, start_row, end_row, code_to_col, day_to_row, day_rows}
    """
    header_rows = _find_header_rows(ws, min_codes=6)
    blocks = []

    for i, hr in enumerate(header_rows):
        start_row = hr + 1
        end_row = (header_rows[i+1] - 1) if i+1 < len(header_rows) else ws.max_row

        # そのヘッダ行の code -> col
        code_to_col = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(hr, c).value
            if _is_code_cell(v):
                code_to_col[v.strip()] = c

        # ブロック内の日付行（A列 or I列が数字）
        day_to_row = {}
        day_rows = []
        for r in range(start_row, end_row + 1):
            a = ws.cell(r, 1).value   # A列（日）
            i9 = ws.cell(r, 9).value  # I列（日）
            if _is_day_cell(a):
                day = int(str(a).strip())
                day_to_row[day] = r
                day_rows.append(r)
            elif _is_day_cell(i9):
                day = int(str(i9).strip())
                day_to_row[day] = r
                day_rows.append(r)

        blocks.append({
            "header_row": hr,
            "start_row": start_row,
            "end_row": end_row,
            "code_to_col": code_to_col,
            "day_to_row": day_to_row,
            "day_rows": day_rows,
        })

    return blocks
JP_WEEK = ["月","火","水","木","金","土","日"]
FILL_SAT = PatternFill("solid", fgColor="FFF200")
FILL_SUN = PatternFill("solid", fgColor="FF4D4D")
FILL_NONE = PatternFill(fill_type=None)

def fill_template_dates_for_blocks(ws, blocks, start_d, end_d, title_text=None):
    # 期間の日付リスト
    days = []
    cur = start_d
    while cur <= end_d:
        days.append(cur)
        cur = cur.fromordinal(cur.toordinal() + 1)

    def fill_for(d):
        wd = d.weekday()
        if wd == 5: return FILL_SAT  # 土
        if wd == 6: return FILL_SUN  # 日
        return FILL_NONE

    for b in blocks:
        # タイトル（ブロックごとに左/右にある想定：A{header_row}, I{header_row}）
        if title_text:
            ws.cell(b["header_row"], 1).value = title_text
            ws.cell(b["header_row"], 9).value = title_text

        rows = b["day_rows"]
        k = min(len(rows), len(days))

        # 期間内を埋める
        for idx in range(k):
            r = rows[idx]
            d = days[idx]
            w = JP_WEEK[d.weekday()]
            f = fill_for(d)

            # 左（日・曜）
            ws.cell(r, 1).value = d.day
            ws.cell(r, 2).value = w
            ws.cell(r, 1).fill = f
            ws.cell(r, 2).fill = f

            # 右（日・曜）
            ws.cell(r, 9).value = d.day
            ws.cell(r, 10).value = w
            ws.cell(r, 9).fill = f
            ws.cell(r, 10).fill = f

        # 余り行をクリア（そのブロックのコード列も消す）
        codes_cols = list(b["code_to_col"].values())
        for idx in range(k, len(rows)):
            r = rows[idx]
            # 日・曜を消す
            for c in (1,2,9,10):
                ws.cell(r, c).value = None
                ws.cell(r, c).fill = FILL_NONE

            # ★その日のスタッフ欄（このブロックが担当するコード列だけ）を消す
            for col in codes_cols:
                ws.cell(r, col).value = None       # 勤務
                ws.cell(r+1, col).value = None     # レジ


def _build_export_matrix(start_d, end_d):
    """
    DBから確定シフトを取得して
    {day(int): {code: {'shift': str, 'regs': str}}} を作る
    """
    qs = (
        ShiftPreference.objects
        .select_related('staff')
        .filter(date__gte=start_d, date__lte=end_d)
        .filter(confirmed_starttime__isnull=False, confirmed_endtime__isnull=False)
    )

    # レジ割当もまとめて取る（related_name='register_assignments'）
    qs = qs.prefetch_related('register_assignments')

    matrix = {}  # day -> code -> {shift, regs}
    missing_codes = set()

    for sh in qs:
        code = (sh.staff.excel_code or '').strip() if sh.staff else ''
        if not code:
            missing_codes.add(sh.staff_id)
            continue

        day = sh.date.day
        matrix.setdefault(day, {})
        matrix[day].setdefault(code, {"shift": "", "regs": ""})

        # 1日1本が基本なので上書きOK（複数が来たら改行で足す等に拡張可）
        matrix[day][code]["shift"] = _fmt_shift_range(sh.confirmed_starttime, sh.confirmed_endtime)

        regs = list(sh.register_assignments.all())
        matrix[day][code]["regs"] = _fmt_regs(regs)

    return matrix, missing_codes

@user_passes_test(lambda u: u.is_superuser)
def excel_export_dashboard(request):
    # デフォルト期間：今日〜今日（必要なら期間候補に合わせて変えてOK）
    start_str = request.GET.get('start')
    end_str = request.GET.get('end')
    start_d = parse_date(start_str) if start_str else None
    end_d = parse_date(end_str) if end_str else None
    if not start_d or not end_d:
        today = dt_date.today()
        start_d = start_d or today
        end_d = end_d or today

    # staffフォームセット
    staff_qs = Staff.objects.order_by('id')
    formset = StaffExcelCodeFormSet(queryset=staff_qs)

    upload_form = ExcelTemplateUploadForm()

    preview = None
    template_codes = []

    # テンプレから利用可能コードを読む（表示用）
    template_path = _get_latest_template_path()
    if template_path:
        wb = openpyxl.load_workbook(template_path)
        ws = wb.active
        blocks = _build_blocks(ws)

        # 全ブロックのコードを集める（重複はsetで潰す）
        all_codes = []
        seen = set()
        for b in blocks:
            for code in b["code_to_col"].keys():
                if code not in seen:
                    seen.add(code)
                    all_codes.append(code)

        template_codes = all_codes  # 並び順をテンプレの並びに合わせたいのでsortedしない
    else:
        messages.warning(request, "テンプレが未設定です（デフォルトテンプレのパスも未設定）。")

    if request.method == 'POST':
        action = request.POST.get('action')

        # 期間
        sd = request.POST.get('start_date')
        ed = request.POST.get('end_date')

        if sd:
            start_d = parse_date(sd)
        if ed:
            end_d = parse_date(ed)


        if action == 'upload_template':
            upload_form = ExcelTemplateUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                upload_form.save()
                messages.success(request, "テンプレをアップロードしました。")
            else:
                messages.error(request, "テンプレのアップロードに失敗しました。xlsxを選んでください。")

        elif action == 'save_codes':
            formset = StaffExcelCodeFormSet(request.POST, queryset=staff_qs)
            print("POST keys:", list(request.POST.keys())[:50])  # 追加
            print("TOTAL_FORMS:", request.POST.get("form-TOTAL_FORMS"))  # 追加
            print("SAMPLE excel_code:", request.POST.get("form-0-excel_code"), request.POST.get("form-1-excel_code"))

            if formset.is_valid():
                print("CLEANED:", [f.cleaned_data.get("excel_code") for f in formset])  # 追加
                
                # 重複チェック（空欄は無視）
                codes = []
                for f in formset:
                    code = (f.cleaned_data.get('excel_code') or '').strip()
                    if code:
                        codes.append(code)
                dup = {c for c in codes if codes.count(c) > 1}
                if dup:
                    messages.error(request, f"excel_code が重複しています: {', '.join(sorted(dup))}")
                else:
                    formset.save()
                    print("action:", request.POST.get("action"))
                    print("TOTAL:", request.POST.get("form-TOTAL_FORMS"))
                    print("HAS 23:", "form-23-excel_code" in request.POST)
                    print("HAS 0 :", "form-0-excel_code" in request.POST)
                    messages.success(request, "スタッフの列割当を保存しました。")
                    return redirect('shiftgenerator:excel-export')
            else:
                print("ERRORS:", formset.errors)  # 追加
                # messages.error(request, "入力にエラーがあります（a〜zの小文字で入力）。")
                messages.error(request, f"入力エラー: {formset.errors}")


        elif action in ('preview', 'download'):
            # テンプレ必須
            template_path = _get_latest_template_path()
            if not template_path:
                messages.error(request, "テンプレが未設定です。先にアップロードしてください。")
            else:
                # マトリクス生成
                matrix, missing = _build_export_matrix(start_d, end_d)
                if missing:
                    messages.warning(request, f"excel_code 未設定のスタッフがいます（staff_id）: {sorted(list(missing))}")

                wb = openpyxl.load_workbook(template_path)
                ws = wb.active

                blocks = _build_blocks(ws)  # ★追加

                # 全コードを template順で抽出
                seen = set()
                template_codes = []
                for b in blocks:
                    for code in b["code_to_col"].keys():
                        if code not in seen:
                            seen.add(code)
                            template_codes.append(code)


                # --- プレビューを作る（get_item不要版） ---
                # 期間の日付（dayだけ）一覧
                preview_dates = []
                cur = start_d
                while cur <= end_d:
                    preview_dates.append(cur)  # date オブジェクト
                    cur = cur.fromordinal(cur.toordinal() + 1)


                pages = []
                for idx, b in enumerate(blocks, start=1):
                    # このブロック内の codes を「ヘッダ列順」に並べる
                    codes = [code for code, _col in sorted(b["code_to_col"].items(), key=lambda x: x[1])]

                    rows = []
                    for d in preview_dates:
                        day = d.day
                        wd = d.weekday()  # 0..6
                        wlabel = JP_WEEK[wd]  # ["月","火",...]
                        work_cells = []
                        reg_cells = []
                        for code in codes:
                            cell = matrix.get(day, {}).get(code, {"shift": "", "regs": ""})
                            work_cells.append(cell.get("shift", ""))
                            reg_cells.append(cell.get("regs", ""))
                        rows.append({
                            "day": day,
                            "weekday": wd,
                            "wlabel": wlabel,
                            "work_cells": work_cells,
                            "reg_cells": reg_cells,
                        })


                    pages.append({
                        "title": f"{idx}ページ目（{codes[0]}〜{codes[-1]}）" if codes else f"{idx}ページ目",
                        "codes": codes,
                        "rows": rows,
                    })

                preview = {
                    "start": start_d,
                    "end": end_d,
                    "pages": pages,
                }



                if action == 'download':
                    # 0) ブロック抽出（a〜m / o〜aa / ab〜… を全部拾う）
                    blocks = _build_blocks(ws)

                    # 1) タイトルを作る（あなたの表記に合わせる）
                    half = "前半" if start_d.day <= 15 else "後半"
                    title = f"{start_d.year%100}.{start_d.month}月{half}"

                    # 2) 全ブロックの日付・土日色・余りクリア・タイトル更新
                    fill_template_dates_for_blocks(ws, blocks, start_d, end_d, title_text=title)
                    blocks = _build_blocks(ws)  # 再構築

                    # 3) code -> (block, col) の索引を作る
                    code_index = {}
                    for b in blocks:
                        for code, col in b["code_to_col"].items():
                            code_index[code] = (b, col)
                    
                    # 4) シフトを書き込み（codeごとに該当ブロックへ）
                    for day, per_code in matrix.items():
                        for code, payload in per_code.items():
                            hit = code_index.get(code)
                            if not hit:
                                miss_code += 1
                                continue

                            b, col = hit
                            r = b["day_to_row"].get(day)
                            if not r:
                                miss_day += 1
                                continue

                            ws.cell(r, col).value = payload["shift"]
                            ws.cell(r, col).alignment = Alignment(
                                horizontal="center",
                                vertical="center"
                            )
                            ws.cell(r + 1, col).value = payload["regs"]
                            ws.cell(r + 1, col).alignment = Alignment(
                                horizontal="center",
                                vertical="center",
                                wrap_text=True
                            )
                    
                    # 5) 返却（ここはそのまま）
                    out = BytesIO()
                    wb.save(out)
                    out.seek(0)
                    filename = f"shift_{start_d.isoformat()}_{end_d.isoformat()}.xlsx"
                    resp = HttpResponse(
                        out.getvalue(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
                    return resp



    context = {
        "formset": formset,
        "upload_form": upload_form,
        "preview": preview,
        "template_codes": template_codes,
        "start_date": start_d,
        "end_date": end_d,
    }
    return render(request, "shiftgenerator/excel_export_dashboard.html", context)