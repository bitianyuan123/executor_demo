#!/usr/bin/env python3
"""Plot uniform cohort comparisons; CPU costs retain the whole-run denominator."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

HERE = Path(__file__).resolve().parent
COLORS = {'queue':'#2463a6', 'outstanding':'#d86e21'}
plt.rcParams.update({'font.family':'DejaVu Sans', 'font.size':10,
    'axes.spines.top':False, 'axes.spines.right':False,
    'svg.hashsalt':'executor-demo-4024', 'savefig.facecolor':'white'})


def save(fig, name):
    fig.savefig(HERE/f'{name}.png', dpi=180)
    fig.savefig(HERE/f'{name}.svg', metadata={'Date':None})
    plt.close(fig)


def comparisons(e):
    fig, axes = plt.subplots(3, 2, figsize=(12, 10.8))
    fig.subplots_adjust(left=.10, right=.97, bottom=.18, top=.82, hspace=.35, wspace=.20)
    fig.suptitle('Same 36 executions: aligned 192-request latency cohorts',
                 x=.08, y=.97, ha='left', fontsize=16, weight='bold')
    fig.text(.08,.925, 'Each point is a candidate / home comparison within one shape, work scale and seed.\n'
             'Latency: planned arrivals in [2, 18) s. CPU cost: whole run, 240 completed requests.\n'
             'No new runs, no confidence intervals, no production workload calibration.', va='top')
    for col, shape in enumerate(('front80','tail4')):
        axes[0,col].set_title('80 x depth 2 + 4 x depth 2' if col==0 else '80 x depth 1 + 4 x depth 22', pad=12)
        for row, (field, label, limits) in enumerate((
            ('cohort_mean_change_percent','Request mean reduction (%)',(-5,55)),
            ('cohort_p99_change_percent','Request p99 reduction (%)',(-25,55)),
            ('whole_cpu_change_percent','Process CPU cost increase (%)',(-2,32)))):
            ax=axes[row,col]
            ax.axhline(0,color='#666',linewidth=.8)
            ax.grid(axis='y',alpha=.2)
            ax.set_ylim(*limits); ax.set_xlim(-.5,2.5)
            ax.set_xticks(range(3),['46,875','375,000','3,000,000'])
            if row==2: ax.set_xlabel('Iterations per business operator')
            if col==0: ax.set_ylabel(label)
            for p in e['pairs']:
                if p['shape']!=shape: continue
                x=(46875,375000,3000000).index(p['iterations'])
                x+=(-.11 if p['policy']=='queue' else .11)+(-.025 if p['seed']==421 else .025)
                value=p[field]*(-1 if row<2 else 1)
                ax.scatter(x,value,color=COLORS[p['policy']],marker='o' if p['seed']==421 else '^',s=45,zorder=3)
    handles=[Line2D([0],[0],color=color,lw=3,label=label) for label,color in (('Q: queue',COLORS['queue']),('O: outstanding',COLORS['outstanding']))]
    handles += [Line2D([0],[0],color='#555',marker=m,lw=0,label=f'Seed {s}') for s,m in ((421,'o'),(422,'^'))]
    fig.legend(handles=handles,loc='lower left',bbox_to_anchor=(.08,.08),ncol=4,frameon=False)
    fig.text(.08,.045,'Positive latency reduction is better; positive CPU increase is a resource cost, not CPU utilization.\n'
             'p99 at n=192 depends on roughly two tail requests. Do not average seeds or treat the spread as uncertainty bounds.',fontsize=9)
    save(fig,'cohort_policy_pairs')


def role_waits(e):
    fig, axes=plt.subplots(1,2,figsize=(12,5.2))
    fig.subplots_adjust(left=.10,right=.97,bottom=.25,top=.73,wspace=.25)
    fig.suptitle('3M tail4: initial fan-out waits fall; successor waits rise',x=.08,y=.97,ha='left',fontsize=15,weight='bold')
    fig.text(.08,.88,'192 completed requests per run; 80 stage0 and 84 ordinary successors per request.\n'
             'Callback wait is submit-after-routing to callback start. It is neither pure Folly Q time nor kernel runqueue time.',va='top',fontsize=9.5)
    index={(int(r['options']['--iterations']),r['name']):r for r in e['runs']}
    for ax,role in zip(axes,('first-wave stage0','ordinary successors')):
        ax.set_title(role); ax.set_ylabel('Mean callback wait (ms)'); ax.grid(axis='y',alpha=.2)
        ax.set_xticks(range(3),['home','Q','O']); ax.set_xlim(-.4,2.4)
        ax.set_ylim(0,14 if role=='first-wave stage0' else .18)
        for x,p in enumerate(('home','queue','outstanding')):
            for seed,shift,m in ((421,-.045,'o'),(422,.045,'^')):
                r=index[(3000000,f'seed-{seed}-tail4-{p}')]['roles'][role]
                ax.scatter(x+shift,r['wait_mean_ms'],color=COLORS.get(p,'#59616c'),marker=m,s=50,zorder=3)
            vals=[index[(3000000,f'seed-{s}-tail4-{p}')]['roles'][role]['wait_mean_ms'] for s in (421,422)]
            ax.annotate(' / '.join(f'{v:.3f}' for v in vals),(x,max(vals)),xytext=(0,9),textcoords='offset points',ha='center',fontsize=9)
    fig.text(.08,.12,'Separate y scales. Values are seed421 / seed422; circles / triangles show the two runs.\n'
             'The aggregates motivate phase x pool attribution. They do not establish cross-request contention or a causal wait penalty.',fontsize=9)
    save(fig,'tail4_role_waits')


if __name__=='__main__':
    evidence=json.loads((HERE/'evidence.json').read_text())
    comparisons(evidence)
    role_waits(evidence)
