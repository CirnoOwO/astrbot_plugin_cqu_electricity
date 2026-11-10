import copy
from decimal import Decimal

import pytest

from cqu_plugin.dashboard_model import make_model
from test_dashboard_model import fake


def data(count=4):
    d=fake(count)
    d['room'].update(campus='shapingba',building='B区3舍',account='B3S624')
    for row in d['captures']:
        row['reading'].update(meter_reading_kwh=None,subsidy_kwh=None,subsidy_balance_yuan='0')
    return d


def test_two_complete_intervals_support_estimate_without_mutating_raw_data():
    d=data(3);before=copy.deepcopy(d);m=make_model(d)
    assert m['forecast']['interval_count']==2
    assert Decimal(m['forecast']['mean_kwh_per_day'])==4
    assert Decimal(m['forecast']['days'])==Decimal('65.68')/Decimal('2.16')
    assert all(Decimal(b['value'])==4 for b in m['reference_bars'] if b['value'] is not None)
    assert d==before


@pytest.mark.parametrize('count',[1,2])
def test_insufficient_intervals_do_not_forecast(count):
    assert make_model(data(count))['forecast'] is None


@pytest.mark.parametrize('kind',['recharge','subsidy','compensated_recharge','price','gap','zero'])
def test_uncertain_intervals_do_not_create_consumption_or_infinite_forecast(kind):
    d=data(3);last=d['captures'][-1]
    if kind=='recharge':last['reading'].update(total_balance_yuan='100',cash_balance_yuan='100')
    elif kind=='subsidy':last['reading']['subsidy_balance_yuan']='10'
    elif kind=='compensated_recharge':
        row=copy.deepcopy(d['captures'][-2]);row.update(id=100,started_at='2030-05-02T18:00:30+08:00',finished_at='2030-05-02T18:00:30+08:00')
        row['reading'].update(total_balance_yuan='100',cash_balance_yuan='100');d['captures'].append(row)
    elif kind=='price':last['reading']['unit_price_yuan']='0.60'
    elif kind=='gap':d['captures'].pop(-2)
    elif kind=='zero':
        for row in d['captures']:row['reading'].update(total_balance_yuan='70',cash_balance_yuan='70')
    m=make_model(d)
    assert m['forecast'] is None
    if kind!='zero':assert m['reference_bars'][-1]['value'] is None


def test_morning_uses_actual_balance_and_previous_complete_daily_intervals():
    d=data(3);row=copy.deepcopy(d['captures'][-1])
    row.update(id=100,started_at='2030-05-04T09:00:30+08:00',finished_at='2030-05-04T09:00:30+08:00')
    row['reading'].update(total_balance_yuan='64.60',cash_balance_yuan='64.60')
    d['captures'].append(row);d['as_of']='2030-05-04T09:01:00+08:00'
    m=make_model(d)
    assert m['forecast']['balance_basis']=='platform_balance'
    assert Decimal(m['forecast']['days'])==Decimal('64.60')/Decimal('2.16')
    assert m['display_balance']=='64.60'
    assert m['reference_bars'][-1]['value'] is None
