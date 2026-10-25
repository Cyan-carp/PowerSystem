"""UTC persistence, named local daily clocks, skipped missed slots (no catch-up)."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import re

def validate_clock(config):
    try:ZoneInfo(config['timezone'])
    except (KeyError,ValueError):raise ValueError('invalid_timezone') from None
    if config['mode']=='interval':
        if type(config['interval_minutes']) is not int or not 15<=config['interval_minutes']<=10080 or config['daily_time'] is not None:raise ValueError('invalid_interval')
    elif config['mode']=='daily':
        if config['interval_minutes'] is not None or not isinstance(config['daily_time'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',config['daily_time']):raise ValueError('invalid_daily_time')
    else:raise ValueError('invalid_clock')

def next_slot(config,after):
    if after.tzinfo is None:raise ValueError('timezone_required')
    after=after.astimezone(timezone.utc)
    if config['mode']=='interval':
        seconds=config['interval_minutes']*60
        return datetime.fromtimestamp((int(after.timestamp())//seconds+1)*seconds,timezone.utc)
    zone=ZoneInfo(config['timezone']);local=after.astimezone(zone);hour,minute=map(int,config['daily_time'].split(':'))
    for day in range(370):
        candidate=(local+timedelta(days=day)).replace(hour=hour,minute=minute,second=0,microsecond=0,fold=0)
        utc=candidate.astimezone(timezone.utc)
        # Spring gaps skip; fall folds use the first occurrence only.
        if utc.astimezone(zone).replace(tzinfo=None)!=candidate.replace(tzinfo=None):continue
        if utc>after:return utc
    raise ValueError('no_valid_slot')

def missed_count(config,first,now):
    if config['mode']=='interval':return int((now-first).total_seconds()//(60*config['interval_minutes']))+1
    count=0;slot=first
    while slot<=now:
        count+=1;slot=next_slot(config,slot)
    return count
