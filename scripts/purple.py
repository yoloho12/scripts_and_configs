#!/usr/bin/python3

import requests
import time
import os
import json
import math
from math import radians, sin, cos
import click

import numpy as np
from sklearn.ensemble import IsolationForest

# Filter sensor data by this interval (sanity check)
PM_25_UPPER_LIMIT = 500 # exclusive
PM_25_LOWER_LIMIT = 0 # inclusive

# Coordinates of place near which we want to find sensors
# To find coordinates of a place:
#    1. On your computer, open Google Maps. If you're using Maps in Lite mode,
#       you'll see a lightning bolt at the bottom and you won't be able to get 
#       the coordinates of a place.
#    2. Right-click the place or area on the map.
#    3. Select What's here?
#    4. At the bottom, you'll see a card with the coordinates.

def distance(lat1, lon1, lat2, lon2):
    R = 6373.0 # Earth radius
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2 #Haversine formula
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# LRAPA correction https://www.lrapa.org/DocumentCenter/View/4147/PurpleAir-Correction-Summary
def LRAPA(x):
    return 0.5*x-0.66

# EPA correction https://cfpub.epa.gov/si/si_public_file_download.cfm?p_download_id=540979&Lab=CEMM
# PM2.5 corrected= 0.52*[PA_cf1(avgAB)] - 0.085*RH +5.71
# x - raw PM2.5 value
# h - humidity
def EPA(x, h):
    return max(0.534 * x - 0.0844 * h + 5.604, 0)

# Calculate AQI for PM2.5.
# https://www3.epa.gov/airnow/aqi-technical-assistance-document-sept2018.pdf

breakpoints=[(0.0  , 12.0,  0,   50),
             (12.1 , 35.4,  51,  100),
             (35.5 , 55.4,  101, 150),
             (55.5 , 150.4, 151, 200),
             (150.5, 250.4, 201, 300),
             (250.5, 350.4, 301, 400),
             (350.5, 500.4, 401, 500)]

def AQI(pm25):
    Cp = round(pm25,1)
    for (Blo,Bhi,Ilo,Ihi) in breakpoints:
        if Cp>=Blo and Cp<=Bhi:
            return ((float(Ihi)-float(Ilo))/(Bhi-Blo))*(Cp-Blo)+float(Ilo)
    return 501 #  "Beyond the AQI"

def get_update_server_list(verbose, sensors_list_ttl, sensors_list_cache_file):
    
    ts = time.time()
    try:
        xts = os.path.getmtime(sensors_list_cache_file)
        with open(sensors_list_cache_file, "r") as f:
            data = json.load(f)
            if verbose:
                print("Server list cache loaded")
    except:
        print("Error reading server list")
        xts = 0.0

    if ts-xts > sensors_list_ttl:
        if verbose:
            print("Fetching server list")
        u = "https://www.purpleair.com/json"
        r = requests.get(u)
        data = r.json()
        with open(sensors_list_cache_file, 'w') as f:
            if verbose:
                print("Saving server list")
            json.dump(data, f)
    return data

@click.command()
@click.option('--verbose', is_flag=True)
@click.option('--dry-run', is_flag=True, help='Do not read or update cache files')
@click.option('--radius', default=5.0, help='Radius in miles')
@click.option('--lat', default=37.256886, help='Lattitude in radians')
@click.option('--lon', default=-122.039156, help='Lattitude in radians')
@click.option('--max-sensors', default=30, help='max number of sensors to query')
@click.option('--sensors-list-ttl', default=1800, help='How often to update sensor list cache (in seconds)')
@click.option('--sensors-list-cache-file', default=os.path.expanduser("~/.purple-all-sensors.list"), help='sensor list cache file location')
@click.option('--results-ttl', default=600, help='How often to update sensor reading (in seconds))')
@click.option('--results-cache-file', default=os.path.expanduser("~/.purple-avg.cache"), help='results cache file location')
@click.option('--max-age', default=10, help='filer out sensors not reporting data given number of minutes')

def purple(verbose, dry_run,
           radius, lat, lon,
           max_sensors, sensors_list_ttl, sensors_list_cache_file,
           results_ttl, results_cache_file,
           max_age
           ):
    mylat = radians(lat)
    mylon = radians(lon)
    
    if verbose:
        print("Coordinates: %f,%f" % (lat,lon))

    data = get_update_server_list(verbose, sensors_list_ttl, sensors_list_cache_file)
    
    if verbose:
        print("Loaded %d sensors" % len(data['results']))

    # filter sensors    
    sensors = []
    for i in data['results']:
        lat = i.get('Lat')
        lon = i.get('Lon')
        if not lat is None and not lon is None:
            d = distance(mylat, mylon, radians(lat), radians(lon))
            if (i.get('DEVICE_LOCATIONTYPE','') == 'outside') and (i['Hidden'] == 'false') and d<radius:
                sensors.append({'id':i['ID'],'distance':d})
                if len(sensors) == max_sensors:
                    break
    if verbose:
        print("Found %d suitable sensors" % len(sensors))

    ts = time.time()

    #TODO: write location to cache file and invalidate it if it changes
    if dry_run:
        xts = 0.0
    else:
        try:
            xts = os.path.getmtime(results_cache_file)
            with open(results_cache_file, "r") as f:
                xv = float(f.readline().strip())
        except:
            print("Error reading cached value")
            xts = 0.0

    if ts-xts < results_ttl:
        if verbose:
            print("Returning cached value")
        print("%.0f" % xv)
        exit(0)

    data = []
    for i in sensors:
        sid = i['id']
        u = "https://www.purpleair.com/json?show=%d"% sid
        r = requests.get(u)
        j = r.json()

        # Channels a, b
        a = j['results'][0]
        b = j['results'][1]
        
        raw0 = float(a['pm2_5_cf_1'])
        raw1 = float(b['pm2_5_cf_1'])
        # humidity only stored for channel a
        humidity = float(a['humidity'])

        # proximity weight
        d = radius - i['distance']
        
        age0 = j['results'][0]['AGE']
        age1 = j['results'][1]['AGE']

        # adding both channels as independent sensors
        if age0 < max_age and raw0>=PM_25_LOWER_LIMIT and raw0<PM_25_UPPER_LIMIT and d>=0:
            data.append((raw0,d,humidity))
        if age1 < max_age and raw1>=PM_25_LOWER_LIMIT and raw1<PM_25_UPPER_LIMIT and d>=0:
            data.append((raw1,d,humidity))

    data = np.array(data)
    v = data[:,0].reshape(-1, 1)
    iso = IsolationForest(contamination=0.01)
    yhat = iso.fit_predict(v)
    mask = yhat != -1
    noutliers = len(mask) - sum(mask)
    if verbose and noutliers>0:
        data = data[mask]
        print("Dropping %d outlier(s)" % noutliers)
        #omask = yhat == -1
        #print(v[omask])

    t  = 0.0
    dt = 0.0

    # x[0] - PM
    # x[1] - distance
    # x[2] - humidity
    for x in data:
        dt = dt + x[1]
        v = EPA(x[0], x[2])
        t = t + (v*x[1])
        
    a = round(AQI(t/dt))
    
    # update timestamp
    if not dry_run:
        with open(results_cache_file, "w") as f:
            f.write(str(a))
            f.write("\n")
        
    print("%.0f" % a)
            
if __name__ == '__main__':
    purple()

    
