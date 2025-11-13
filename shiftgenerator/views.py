from django.http import HttpResponse, JsonResponse, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.conf import settings
from django.contrib.auth import login as auth_login, authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib import messages
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time, parse_datetime
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.timezone import now, localdate

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

from .forms import CustomUserCreationForm, ShiftPreferenceForm
from .models import ShiftPreference, Staff, DayOfWeek, ShiftRegisterAssignment, ShiftHistory, Holiday, Skill, StaffSkill, ShiftSubmissionPeriod, ShiftSubmission
from django.contrib.auth.hashers import make_password
from django.contrib import messages
from django.db.models import Exists, OuterRef, Q
from shiftgenerator.utils.utils import close_expired_periods





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

        if has_duplicate_period(type_, start_date, end_date, start_time, end_time):
            messages.error(request, "重複している募集期間がすでに存在します。")
            return redirect("shiftgenerator:shift-management-view")
        
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
    all_periods = ShiftSubmissionPeriod.objects.all().order_by('-start_date')

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

        "all_periods": all_periods,
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
    auto_close = request.POST.get("auto_close_date")
    period.is_active = True
    if auto_close:                 # 空ならそのまま
        period.auto_close_date = parse_datetime(auto_close)
    period.save()
    messages.success(request, "募集期間を再開しました。")
    return redirect('shiftgenerator:shift-management-view')



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

         # 有効スタッフ（is_active=True）は全員表示
    active_staff = list(Staff.objects.filter(is_active=True))

    # 無効スタッフ（is_active=False）はその日「確定シフト」がある場合だけ表示
    inactive_staff = list(
        Staff.objects.filter(is_active=False).filter(
            Exists(
                ShiftPreference.objects.filter(
                    staff=OuterRef('pk'),
                    date=target_date,
                    confirmed_starttime__isnull=False,
                    confirmed_endtime__isnull=False
                )
            )
        )
    )

    # Pythonで合体＆ID順ソート
    staff = sorted(active_staff + inactive_staff, key=lambda s: s.id)

    for preference in preferences:
        preference.confirmed_starttime = datetime.combine(preference.date, preference.confirmed_starttime)
        preference.confirmed_endtime = datetime.combine(preference.date, preference.confirmed_endtime)

    register_assignments = ShiftRegisterAssignment.objects.all()
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
    }
    return render(request, 'shiftgenerator/shift_management.html', context)

@login_required
@require_POST
@transaction.atomic
def bulk_reset_to_wish(request):
    """
    指定した日付範囲のシフトを
      - いったん確定シフト＋レジを全部クリア
      - そのあと「希望(starttime/endtime)」から確定(confirmed_*)を作り直す
    staff_id があればその人だけ、無ければ全員（is_active=True）対象。
    """
    import json
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "JSON が不正です。"}, status=400)

    start_str = payload.get("start")
    end_str   = payload.get("end")
    staff_id  = payload.get("staff_id")  # None または int

    start = parse_date(start_str) if start_str else None
    end   = parse_date(end_str)   if end_str   else None

    if not start or not end or start > end:
        return JsonResponse({"success": False, "error": "日付範囲が不正です。"}, status=400)

    # 対象スタッフ
    staff_qs = Staff.objects.filter(is_active=True)
    if staff_id is not None:
        staff_qs = staff_qs.filter(id=staff_id)

    if not staff_qs.exists():
        return JsonResponse({"success": False, "error": "対象スタッフが見つかりません。"}, status=404)

    total_reset = 0

    cur = start
    from datetime import timedelta
    while cur <= end:
        # この日＋対象スタッフの ShiftPreference 全体
        base_qs = ShiftPreference.objects.filter(date=cur, staff__in=staff_qs)

        # ① 確定シフトに紐づくレジを全部削除
        confirmed_qs = base_qs.filter(
            confirmed_starttime__isnull=False,
            confirmed_endtime__isnull=False,
        )
        shift_ids = list(confirmed_qs.values_list("id", flat=True))
        if shift_ids:
            ShiftRegisterAssignment.objects.filter(shift_id__in=shift_ids).delete()

        # ② いったん確定時間を全部クリア
        confirmed_qs.update(confirmed_starttime=None, confirmed_endtime=None)

        # ③ 希望(starttime/endtime がある行)を「確定」にコピー
        wish_qs = base_qs.filter(
            starttime__isnull=False,
            endtime__isnull=False,
        )
        for p in wish_qs:
            p.confirmed_starttime = p.starttime
            p.confirmed_endtime   = p.endtime
            p.save(update_fields=["confirmed_starttime", "confirmed_endtime"])
            total_reset += 1

        cur += timedelta(days=1)

    return JsonResponse({"success": True, "count": total_reset})
@login_required
@require_GET
def api_shift_day(request):
    date_str = request.GET.get('date')
    if not date_str:
        return JsonResponse({'error': 'date is required'}, status=400)

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'invalid date'}, status=400)

    # スタッフ一覧
    staff_qs = Staff.objects.filter(is_active=True).order_by('id')
    staff = [{'id': s.id, 'name': s.name} for s in staff_qs]

    # helper: 日付と time/datetime を "YYYY-MM-DDTHH:MM:SS" に整形（tz変換しない）
    def to_iso(dt_date, t_or_dt):
        if t_or_dt is None:
            return None
        if isinstance(t_or_dt, time):
            return f"{dt_date.isoformat()}T{t_or_dt.strftime('%H:%M:%S')}"
        if isinstance(t_or_dt, datetime):
            return t_or_dt.strftime('%Y-%m-%dT%H:%M:%S')
        # 想定外は文字列化して落ちないようにする
        return str(t_or_dt)

    # 確定シフト（confirmed_* が埋まっているもの）
    prefs = (
        ShiftPreference.objects
        .filter(
            date=target_date,
            confirmed_starttime__isnull=False,
            confirmed_endtime__isnull=False,
        )
        .select_related('staff')
        .order_by('staff_id', 'confirmed_starttime')
    )

    preferences = [{
        'id': p.id,
        'staff_id': p.staff_id,
        'confirmed_starttime': to_iso(p.date, p.confirmed_starttime),
        'confirmed_endtime':   to_iso(p.date, p.confirmed_endtime),
    } for p in prefs]

    # 希望（背景）
    wishes_qs = (ShiftPreference.objects
                 .filter(date=target_date)
                 .select_related('staff'))

    wish_preferences = [{
        'id': w.id,
        'staff_id': w.staff_id,
        'date': w.date.strftime('%Y-%m-%d'),
        'starttime': w.starttime.strftime('%H:%M:%S') if w.starttime else None,
        'endtime':   w.endtime.strftime('%H:%M:%S')   if w.endtime   else None,
    } for w in wishes_qs if w.starttime and w.endtime]

    # レジ帯（related_name=register_assignments 前提）
    registers_by_shift = {}
    for p in prefs:
        regs = p.register_assignments.all().order_by('register_start_time')
        registers_by_shift[str(p.id)] = [{
            'id': r.id,
            'register_number': r.register_number,
            'register_start_time': r.register_start_time.strftime('%H:%M') if r.register_start_time else None,
            'register_end_time':   r.register_end_time.strftime('%H:%M')   if r.register_end_time   else None,
        } for r in regs]

    return JsonResponse({
        'date': target_date.isoformat(),
        'staff': staff,
        'preferences': preferences,
        'wish_preferences': wish_preferences,
        'registers_by_shift': registers_by_shift,
    })

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
        'active_periods': active_periods,
        'events': events_json,
        'name': staff_profile.name,
        'username': user.username,
        'history': history_json if history_list else None
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
        ).order_by('date', 'starttime', 'endtime', 'holiday_id')
        
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

        prev_submission = (
            ShiftSubmission.objects.filter(staff=staff_profile, period=period)
            .order_by('-updated_at').first()
        )
        if prev_submission and prev_submission.snapshot:
            if current_prefs == prev_submission.snapshot:
                # 完全一致なら「変更なし」
                return JsonResponse({
                    'success': False,
                    'no_change': True,
                    'message': '前回提出内容と全く同じです。'
                })

        status = "再提出" if prev_submission else "初回"
        comment = request.POST.get("period_comment", "")
        ShiftSubmission.objects.create(
            staff=staff_profile,
            period=period,
            comment=comment,
            submission_status=status,
            snapshot=current_prefs  # ← すべて文字列なのでJSONでOK！
        )
        return JsonResponse({
            'success': True,
            'period_label': period.label,
            'status': status
        })

    return JsonResponse({'success': False, 'error': '無効なリクエストです。'})


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
            new_shift = ShiftPreference.objects.create(
                staff=staff_profile,
                starttime=starttime,
                endtime=endtime,
                date=date,
                day_of_week=day_of_week_instance
            )
            
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
    print(events)


    return JsonResponse({'success': True, 'events': events}, safe=False)
