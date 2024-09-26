from django.urls import path
from . import views


app_name = 'shiftgenerator'

urlpatterns = [
    path('', views.index, name='index'),
    path('shift-generate/', views.shift_generate, name='shift-generate'),
    path('shift-results/', views.shift_results, name='shift-results'),
    path('shift-form/', views.shift_form, name='shift-form'),
    path('shift-register/', views.shift_register, name='shift-register'),
    path('new-register-shift/', views.new_register_shift, name='new-register-shift'),
    path('shift-detail/<int:shift_id>/', views.shift_detail, name='shift-detail'),
    path('get-shift-history/', views.get_shift_history, name='get-shift-history'),
    path('get-holidays/', views.get_holidays, name='get-holidays'),
    path('history-shift-register/', views.history_shift_register, name='history-shift-register'),
    path('holiday-shift-register/', views.holiday_shift_register, name='holiday-shift-register'),
    path('get-updated-events/', views.get_updated_events, name='get-updated-events'),
    path('register/', views.register, name='register'),
    path('login/', views.login, name='login'), 
    path('shift-management-view/', views.shift_management_view, name='shift-management-view'),
    path('shift-management/', views.shift_management, name='shift-management'),
    path('copy-shifts/', views.copy_shifts, name='copy-shifts'),
    path('save-shifts/', views.save_shifts, name='save-shifts'),

]
