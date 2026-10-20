/* SPDX-License-Identifier: GPL-3.0-only
 * Survey Adjustment experimental receiver-calibration extension, 2026.
 * Upstream RTKLIB remains separately attributed under its BSD-2-Clause terms.
 * This file is built into a separately identified local engine, not the plugin.
 */
#include "rtklib.h"

int sa_system_index(int sys)
{
    return sys==SYS_GPS?0:sys==SYS_GLO?1:sys==SYS_GAL?2:sys==SYS_CMP?3:-1;
}

int sa_antmodel(const pcv_t *pcv,const double *del,const double *azel,
                int enabled,int sys,int nf,double *dant)
{
    const int k=sa_system_index(sys);
    const double z=(90.0-azel[1]*R2D)/5.0;
    const double unit[3]={sin(azel[0])*cos(azel[1]),cos(azel[0])*cos(azel[1]),sin(azel[1])};
    int f,j,lo=z<0?0:z>=18?18:(int)z;
    static unsigned int logged[2][4]={{0}};
    const int receiver=strstr(pcv->type,"SA_BASE")?1:0;
    const double part=z<=0||z>=18?0:z-lo;
    if (enabled && (k<0||!pcv->type[0])) return 0;
    for (f=0;f<NFREQ;f++) {
        double pcv_value=0;
        dant[f]=0;
        if (enabled && f<nf) {
            if (!(pcv->sa_present[k]&(1u<<f))) return 0;
            if (!(logged[receiver][k]&(1u<<f))) {
                fprintf(stderr,"SA_PCV,%s,%c,%d,%.9f,%.9f,%.9f,%.9f,%.9f\n",
                    pcv->type,"GREC"[k],f,pcv->sa_off[k][f][0],pcv->sa_off[k][f][1],pcv->sa_off[k][f][2],
                    pcv->sa_var[k][f][0],pcv->sa_var[k][f][18]);
                logged[receiver][k]|=1u<<f;
            }
            pcv_value=pcv->sa_var[k][f][lo];
            if (lo<18) pcv_value+=(pcv->sa_var[k][f][lo+1]-pcv_value)*part;
        }
        for (j=0;j<3;j++) dant[f]-=(del[j]+(enabled&&f<nf?pcv->sa_off[k][f][j]:0))*unit[j];
        dant[f]+=pcv_value;
    }
    return 1;
}
