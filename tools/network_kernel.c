/* SPDX-License-Identifier: GPL-3.0-only
 * Survey Adjustment experimental network geometry / integer-search kernel.
 * Own wrapper; calls separately reviewed RTKLIB by T. Takasu et al.
 * RTKLIB BSD-2-Clause and SOFA notices remain with its separate source/build.
 * Not an upstream release. No network estimator or survey accuracy claim here.
 */
#include "rtklib.h"
#include <stdarg.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int showmsg(const char *format, ...) { return 0; }
void settspan(gtime_t a,gtime_t b) {}
void settime(gtime_t a) {}

static int integers(int argc,char **argv) {
    if(argc!=5)return 11;
    int n=atoi(argv[2]);if(n<1||n>64)return 12;
    FILE *in=fopen(argv[3],"r");if(!in)return 13;
    double *a=mat(n,1),*q=mat(n,n),*f=mat(n,2),s[2];
    for(int i=0;i<n;i++)if(fscanf(in,"%lf",a+i)!=1||!isfinite(a[i]))return 14;
    for(int i=0;i<n*n;i++)if(fscanf(in,"%lf",q+i)!=1||!isfinite(q[i]))return 15;
    fclose(in);int rc=lambda(n,2,a,q,f,s);if(rc)return 16;
    FILE *out=fopen(argv[4],"w");if(!out)return 17;
    fprintf(out,"SA_NETWORK_LAMBDA_V1,%d,%.17g,%.17g\n",n,s[0],s[1]);
    for(int j=0;j<2;j++){for(int i=0;i<n;i++)fprintf(out,"%.0f%c",f[i+j*n],i==n-1?'\n':',');}
    fclose(out);free(a);free(q);free(f);return 0;
}

static int dump(int argc,char **argv) {
    /* dump OBS NAV receiver-ANTEX XYZ virtual-XYZ output.csv */
    if(argc!=12)return 21;
    obs_t observations={0};nav_t nav={0};sta_t sta={0};pcvs_t pcvs={0};gtime_t zero={0};
    double rr[3],virtual_rr[3],pos[3],vpos[3],vertical[]={0,PI/2};
    for(int i=0;i<3;i++){rr[i]=atof(argv[5+i]);virtual_rr[i]=atof(argv[8+i]);}
    if(norm(rr,3)<6e6||norm(rr,3)>7e6||norm(virtual_rr,3)<6e6||norm(virtual_rr,3)>7e6)return 22;
    setcodepri(SYS_GPS,1,"C");setcodepri(SYS_GPS,2,"W");
    if(readrnxt(argv[3],1,zero,zero,0,"",&observations,&nav,NULL)<=0)return 23;
    if(readrnxt(argv[2],1,zero,zero,0,"-GL1C -GL2W",&observations,&nav,&sta)<=0)return 24;
    if(nav.n<1||observations.n<1||observations.n>1000000)return 25;
    if(!readpcv(argv[4],&pcvs))return 26;
    pcv_t *model=searchpcv(0,sta.antdes,observations.data[0].time,&pcvs);
    if(!model||strncmp(model->type,sta.antdes,20)!=0)return 27;
    ecef2pos(rr,pos);ecef2pos(virtual_rr,vpos);
    FILE *out=fopen(argv[11],"w");if(!out)return 28;
    fprintf(out,"week,tow,sat,P1,L1,P2,L2,D1,D2,lli1,lli2,elevation_deg,model1,model2,virtual_model,spp_clock_s\n");
    prcopt_t opt=prcopt_default;opt.navsys=SYS_GPS;opt.nf=2;opt.elmin=15*D2R;
    for(int first=0;first<observations.n;) {
        int end=first+1;
        while(end<observations.n&&fabs(timediff(observations.data[end].time,observations.data[first].time))<1e-9)end++;
        int count=end-first;if(count>MAXOBS)return 29;
        obsd_t *obs=observations.data+first;
        sol_t sol={0};ssat_t ssat[MAXSAT]={0};char message[512]={0};double azel[MAXOBS*2];
        int spp_ok=pntpos(obs,count,&nav,&opt,&sol,azel,ssat,message);
        double rs[MAXOBS*6],dts[MAXOBS*2],vars[MAXOBS];int health[MAXOBS];
        satposs(obs[0].time,obs,count,&nav,EPHOPT_BRDC,rs,dts,vars,health);
        for(int i=0;i<count;i++) {
            obsd_t *o=obs+i;int prn;
            if(satsys(o->sat,&prn)!=SYS_GPS||o->code[0]!=CODE_L1C||o->code[1]!=CODE_L2W||
                !o->P[0]||!o->P[1]||!o->L[0]||!o->L[1]||health[i]!=0||norm(rs+i*6,3)<1e7)continue;
            double los[3],direction[2],ant[NFREQ]={0};
            double rho=geodist(rs+i*6,rr,los);satazel(pos,los,direction);
            if(direction[1]<15*D2R)continue;
            antmodel(model,sta.del,direction,1,ant);
            double dry=tropmodel(o->time,pos,vertical,0)*tropmapf(o->time,pos,direction,NULL);
            double range=rho-CLIGHT*dts[i*2]+dry;
            /* Iterative virtual code transmit time, master receiver clock kept.
               No physical antenna or marker-to-ARP offset at virtual point. */
            obsd_t vo=*o;double vrs[6],vclk[2],variance;int unhealthy;
            vo.P[0]=o->P[0]+geodist(rs+i*6,virtual_rr,los)-rho;
            double virtual_range=0;
            for(int pass=0;pass<4;pass++) {
                satposs(vo.time,&vo,1,&nav,EPHOPT_BRDC,vrs,vclk,&variance,&unhealthy);
                double vrho=geodist(vrs,virtual_rr,los);satazel(vpos,los,direction);
                double vdry=tropmodel(vo.time,vpos,vertical,0)*tropmapf(vo.time,vpos,direction,NULL);
                virtual_range=vrho-CLIGHT*vclk[0]+vdry;
                vo.P[0]=o->P[0]+virtual_range-(range+ant[0]);
            }
            if(unhealthy!=0||!isfinite(virtual_range))continue;
            int week;double tow=time2gpst(o->time,&week);char sat[16];satno2id(o->sat,sat);
            double elevation;geodist(rs+i*6,rr,los);elevation=satazel(pos,los,direction)*R2D;
            fprintf(out,"%d,%.9f,%s,%.9f,%.9f,%.9f,%.9f,%.6f,%.6f,%u,%u,%.9f,%.9f,%.9f,%.9f,%.12g\n",
                week,tow,sat,o->P[0],o->L[0],o->P[1],o->L[1],o->D[0],o->D[1],o->LLI[0],o->LLI[1],elevation,
                range+ant[0],range+ant[1],virtual_range,spp_ok?sol.dtr[0]:NAN);
        }
        first=end;
    }
    fclose(out);freeobs(&observations);freenav(&nav,0xFF);free_pcvs(&pcvs);return 0;
}

int main(int argc,char **argv) {
    if(argc<2)return 1;
    if(strcmp(argv[1],"identity")==0){puts("SA_NETWORK_KERNEL_V1; RTKLIB EX 2.5.1; GPS 1C/2W; broadcast; receiver NOAZI; independent lambda search");return 0;}
    if(strcmp(argv[1],"lambda")==0)return integers(argc,argv);
    if(strcmp(argv[1],"dump")==0)return dump(argc,argv);
    return 2;
}
