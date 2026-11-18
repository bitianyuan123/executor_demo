#!/usr/bin/env python3
"""Render descriptive historical service summaries, not online overload metrics."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

HERE=Path(__file__).resolve().parent
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,
    'axes.spines.top':False,'axes.spines.right':False,'svg.hashsalt':'executor-demo-4025'})
e=json.loads((HERE/'evidence.json').read_text())
fig,axes=plt.subplots(1,2,figsize=(12.8,6.2))
fig.subplots_adjust(left=.07,right=.98,bottom=.25,top=.75,wspace=.25)
fig.suptitle('Historical unbound pools: frequent short segments, service-heavy long segments',
             x=.06,y=.97,ha='left',fontsize=14,weight='bold')
fig.text(.06,.89,'One 10 s capture; high-service bin = S > 2 x own full-window mean. S is matched CPU service / 1 ms.\n'
         'Historical four pools describe this capture only. The target remains one pool per verified cluster.',va='top',fontsize=10)
g=e['pooled_duration_groups']; x=np.arange(4)
for dx,field,label,color in ((-.19,'count_percent','Share of 1,115 segments','#2463a6'),
                             (.19,'area_cpu_ms_percent','Share of high-segment CPU service','#d86e21')):
    vals=[r[field] for r in g]
    axes[0].bar(x+dx,vals,.36,label=label,color=color)
    for xi,v in zip(x+dx,vals):axes[0].text(xi,v+.8,f'{v:.1f}',ha='center',fontsize=9)
axes[0].set_xticks(x,['(0, 2]','(2, 5]','(5, 20]','>20'])
axes[0].set_xlabel('Consecutive high-service bin duration (ms)')
axes[0].set_ylabel('Share (%)');axes[0].set_ylim(0,70)
axes[0].legend(loc='upper left',bbox_to_anchor=(0,-.19),fontsize=8,frameon=False)
ps=list(e['pools'].values())
low=[p['nonhigh_area_percent'] for p in ps]
axes[1].bar(x,low,.6,color='#31966a',label='Service outside high bins')
axes[1].bar(x,[100-v for v in low],.6,bottom=low,color='#a5b6c9',label='Service in high bins')
for i,v in enumerate(low):axes[1].text(i,v/2,f'{v:.1f}%',ha='center',va='center',color='white')
axes[1].set_xticks(x,['pool a','pool b','pool c','pool d']);axes[1].set_ylim(0,100)
axes[1].set_ylabel('Share of each historical pool CPU service (%)')
axes[1].legend(loc='upper left',bbox_to_anchor=(0,-.19),fontsize=8,frameon=False)
for ax in axes:ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True)
fig.text(.06,.055,'Non-high service is not an identified background task or request tail. Segment duration is not remaining queue delay.\n'
         'Unknown trace coverage and inferred thread names remain limitations; none of these thresholds are online routing settings.',fontsize=9)
fig.savefig(HERE/'historical_service_shapes.png',dpi=180)
fig.savefig(HERE/'historical_service_shapes.svg',metadata={'Date':None})
