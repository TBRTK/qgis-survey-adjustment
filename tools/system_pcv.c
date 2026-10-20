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
                int enabled,int sys,int nf,
#ifdef SURVEY_SA2
                const uint8_t *codes,double heading,
#endif
                double *dant)
{
    const int k=sa_system_index(sys);
    const double z=(90.0-azel[1]*R2D)/5.0;
    const double unit[3]={sin(azel[0])*cos(azel[1]),cos(azel[0])*cos(azel[1]),sin(azel[1])};
    int f,j,lo=z<0?0:z>=18?18:(int)z;
    static unsigned int logged[2][4]={{0}};
    const int receiver=strstr(pcv->type,"SA_BASE")?1:0;
    const double part=z<=0||z>=18?0:z-lo;
#ifdef SURVEY_SA2
    if (k<0) return 0;
#endif
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
#ifdef SURVEY_SA2
                int a,q;
                fprintf(stderr,"SA_ORIENT,%s,%c,%d,%.9f,%.9f\n",pcv->type,"GREC"[k],f,heading,pcv->sa_dazi);
                if (pcv->sa_dazi>0) for (a=0;a<=lround(360/pcv->sa_dazi);a++) {
                    fprintf(stderr,"SA_AZI,%s,%c,%d,%d",pcv->type,"GREC"[k],f,a);
                    for (q=0;q<19;q++) fprintf(stderr,",%.9f",pcv->sa_azi[k][f][a][q]);
                    fprintf(stderr,"\n");
                }
#endif
                logged[receiver][k]|=1u<<f;
            }
            pcv_value=pcv->sa_var[k][f][lo];
            if (lo<18) pcv_value+=(pcv->sa_var[k][f][lo+1]-pcv_value)*part;
#ifdef SURVEY_SA2
            if (pcv->sa_dazi>0) {
                double a=fmod(azel[0]*R2D-heading+720.0,360.0)/pcv->sa_dazi;
                int lower=(int)a; double weight=a-lower;
                if (lower<0||lower>=72||pcv->sa_rows[k][f]!=(int)lround(360/pcv->sa_dazi)+1) return 0;
                double first=pcv->sa_azi[k][f][lower][lo];
                double second=pcv->sa_azi[k][f][lower+1][lo];
                if (lo<18) {
                    first+=(pcv->sa_azi[k][f][lower][lo+1]-first)*part;
                    second+=(pcv->sa_azi[k][f][lower+1][lo+1]-second)*part;
                }
                pcv_value=first+(second-first)*weight;
            }
#endif
        }
#ifdef SURVEY_SA2
        {
            double e=enabled&&f<nf?pcv->sa_off[k][f][0]:0;
            double n=enabled&&f<nf?pcv->sa_off[k][f][1]:0;
            double angle=heading*D2R;
            double offset[3]={e*cos(angle)+n*sin(angle),n*cos(angle)-e*sin(angle),enabled&&f<nf?pcv->sa_off[k][f][2]:0};
            for (j=0;j<3;j++) dant[f]-=(del[j]+offset[j])*unit[j];
        }
#else
        for (j=0;j<3;j++) dant[f]-=(del[j]+(enabled&&f<nf?pcv->sa_off[k][f][j]:0))*unit[j];
#endif
        dant[f]+=pcv_value;
#ifdef SURVEY_SA2
        if (f<nf&&codes[f]) {
            const char *code=code2obs(codes[f]);
            char band=(sys==SYS_GAL?(f?'5':'1'):sys==SYS_CMP?(f?'6':'1'):(f?'2':'1'));
            if (code[0]!=band || (sys==SYS_CMP&&f==0&&!strchr("DPX",code[1]))) return 0;
            static unsigned int applied[2][4]={{0}};
            if (!(applied[receiver][k]&(1u<<f))) {
                fprintf(stderr,"SA_APPLY,%s,%c,%d,%s,%.9f,%.9f,%.9f\n",pcv->type,"GREC"[k],f,code,azel[0]*R2D,90-azel[1]*R2D,dant[f]);
                applied[receiver][k]|=1u<<f;
            }
        }
#endif
    }
    return 1;
}
