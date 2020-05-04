#!/bin/bash
# Copyright 2018 The MLPerf Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

sampling_interval=2
sampling_duration=60

csvfile=sample_metrics_$(hostname -s)_$(date '+%Y-%m-%d_%H-%M-%S')_${sampling_interval}_${sampling_duration}.csv

#echo \
python3 sample_metrics.py -v \
        -I $sampling_interval \
        -D $sampling_duration \
        -c $csvfile \
        samplers.yokogawa

