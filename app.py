from __future__ import annotations

import base64, gzip, io, json
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from metrics_data import METRICS_GZ_B64
from series_data_1 import SERIES_GZ_B64_1
from series_data_2 import SERIES_GZ_B64_2
from series_data_3 import SERIES_GZ_B64_3

SERIES_GZ_B64 = SERIES_GZ_B64_1 + SERIES_GZ_B64_2 + SERIES_GZ_B64_3

st.set_page_config(page_title='Tracking Error Peers · BCI', page_icon='📊', layout='wide')
NAVY='#003578'; BLUE='#0B63C5'; BG='#F8FAFC'; BORDER='#E2E8F0'; TEXT='#475569'; GREEN='#0E7C57'; RED='#C0392B'
st.markdown(f'''<style>
.main .block-container{{max-width:96%;padding-top:1.2rem}}
.bci-header{{background:linear-gradient(135deg,{NAVY},#00224d);padding:1.35rem 1.7rem;border-radius:12px;color:white;margin-bottom:1rem}}
.bci-header h1{{color:white!important;margin:0!important;font-size:1.8rem!important}} .bci-header p{{color:#B9D4F2;margin:.35rem 0 0}}
.card{{background:white;border:1px solid {BORDER};border-top:3px solid {NAVY};border-radius:10px;padding:1rem 1.1rem}}
.klabel{{font-size:.75rem;text-transform:uppercase;color:{TEXT};font-weight:700}} .kval{{font-size:1.55rem;color:{NAVY};font-weight:750}}
</style>''', unsafe_allow_html=True)

@st.cache_data
def load_data():
    metrics=json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode('utf-8'))
    series=json.loads(gzip.decompress(base64.b64decode(SERIES_GZ_B64)).decode('utf-8'))
    df=pd.DataFrame(metrics)
    for c in ['te_ewma_anual','te_equiponderado_anual','IR','ret_1y_fondo','ret_1y_pares','vol_anual','exceso_1y']:
        df[c]=pd.to_numeric(df[c],errors='coerce')
    df['es_bci']=df['es_bci'].astype(bool)
    return df,series

def pct(x,d=2,sign=False):
    if pd.isna(x): return 'N/D'
    return (f'{x*100:+.{d}f}' if sign else f'{x*100:.{d}f}').replace('.',',')+'%'

def num(x,d=2,sign=False):
    if pd.isna(x): return 'N/D'
    return (f'{x:+.{d}f}' if sign else f'{x:.{d}f}').replace('.',',')

df,series=load_data()
cut=pd.to_datetime(df['fecha_corte']).max().strftime('%d-%m-%Y')

st.sidebar.markdown('## 🏢 BCI Asset Management')
st.sidebar.caption('Riesgo de Mercado · Monitoreo Peers')
view=st.sidebar.radio('Vista',['📊 Dashboard por categoría','🏢 Resumen ejecutivo','🔍 Alfa vs Tracking Error','⚔️ BCI vs Peer','📥 Datos'])
groups=['Todos']+sorted(df.grupo.dropna().unique().tolist())
group=st.sidebar.selectbox('Grupo',groups)
base=df if group=='Todos' else df[df.grupo==group]
cats=sorted(base.categoria.unique())
cat=st.sidebar.selectbox('Categoría / Peer Group',cats)
st.sidebar.info(f'52 semanas · EWMA λ=0,94 · anualización √52 · corte {cut}')

st.markdown(f'''<div class="bci-header"><h1>Tracking Error Peers · Leave-One-Out</h1><p>Fondo BCI versus promedio de los demás fondos de cada peer group · corte {cut}</p></div>''',unsafe_allow_html=True)

if view=='📊 Dashboard por categoría':
    d=df[df.categoria==cat].sort_values('te_ewma_anual',ascending=False).copy()
    b=d[d.es_bci].iloc[0] if d.es_bci.any() else None
    st.subheader(cat)
    if b is not None:
        med=d.te_ewma_anual.median(); rank=int(b['rank']); n=len(d)
        cols=st.columns(5)
        vals=[('TE EWMA BCI',pct(b.te_ewma_anual,4)),('Ranking',f'#{rank} de {n}'),('Exceso 1A',pct(b.exceso_1y,2,True)),('Information Ratio',num(b.IR,2,True)),('Volatilidad',pct(b.vol_anual,2))]
        for c,(lab,val) in zip(cols,vals):
            c.markdown(f'<div class="card"><div class="klabel">{lab}</div><div class="kval">{val}</div></div>',unsafe_allow_html=True)
        fig=px.bar(d,x='te_ewma_anual',y='fondo',orientation='h',color='es_bci',color_discrete_map={True:NAVY,False:'#94A3B8'},hover_data=['run','IR','exceso_1y'])
        fig.add_vline(x=med,line_dash='dash',line_color=BLUE,annotation_text='Mediana')
        fig.update_layout(height=max(390,42*len(d)),showlegend=False,xaxis_tickformat='.2%',yaxis_title='',xaxis_title='Tracking Error EWMA anualizado')
        st.plotly_chart(fig,use_container_width=True)

    stem=d.archivo.iloc[0]
    hist=series.get(stem)
    if hist:
        h=pd.DataFrame(hist['valores'],index=pd.to_datetime(hist['fechas']))
        cols=[c for c in h.columns if c in d.fondo.astype(str).tolist()]
        if cols: h=h[cols]
        fig2=px.line(h,title='Series históricas base 100 · 52 semanas')
        fig2.update_layout(height=430,xaxis_title='',yaxis_title='Índice base 100',legend_title='Fondo')
        st.plotly_chart(fig2,use_container_width=True)
    show=d[['fondo','run','es_bci','te_ewma_anual','exceso_1y','IR','vol_anual','rank']].copy()
    show.columns=['Fondo','RUN','BCI','TE EWMA','Exceso 1A','IR','Volatilidad','Ranking']
    st.dataframe(show,use_container_width=True,hide_index=True,column_config={'TE EWMA':st.column_config.NumberColumn(format='%.4f'),'Exceso 1A':st.column_config.NumberColumn(format='%.4f'),'Volatilidad':st.column_config.NumberColumn(format='%.4f')})

elif view=='🏢 Resumen ejecutivo':
    rows=[]
    for c,g in df.groupby('categoria'):
        if not g.es_bci.any(): continue
        b=g[g.es_bci].iloc[0]
        rows.append({'Categoría':c,'Grupo':b.grupo,'Fondo BCI':b.fondo,'TE BCI':b.te_ewma_anual,'Mediana peers':g.te_ewma_anual.median(),'Ranking':int(b['rank']),'N':len(g),'Exceso 1A':b.exceso_1y,'IR':b.IR})
    r=pd.DataFrame(rows).sort_values('TE BCI',ascending=False)
    a,b,c,d=st.columns(4)
    a.metric('Categorías',len(r)); b.metric('Fondos mercado',len(df)); c.metric('BCI bajo mediana',int((r['TE BCI']<r['Mediana peers']).sum())); d.metric('Exceso positivo',int((r['Exceso 1A']>0).sum()))
    fig=go.Figure()
    fig.add_bar(name='BCI',x=r['Categoría'],y=r['TE BCI'])
    fig.add_scatter(name='Mediana peers',x=r['Categoría'],y=r['Mediana peers'],mode='markers')
    fig.update_layout(height=480,yaxis_tickformat='.2%',xaxis_tickangle=-35,yaxis_title='Tracking Error')
    st.plotly_chart(fig,use_container_width=True)
    st.dataframe(r,use_container_width=True,hide_index=True)

elif view=='🔍 Alfa vs Tracking Error':
    fig=px.scatter(df,x='te_ewma_anual',y='exceso_1y',color='categoria',symbol='es_bci',symbol_map={True:'star',False:'circle'},hover_name='fondo',hover_data=['run','IR','vol_anual'])
    fig.add_hline(y=0,line_color='#64748B')
    fig.update_layout(height=650,xaxis_tickformat='.2%',yaxis_tickformat='.2%',xaxis_title='Tracking Error EWMA',yaxis_title='Exceso de retorno 1A vs peers')
    st.plotly_chart(fig,use_container_width=True)

elif view=='⚔️ BCI vs Peer':
    d=df[df.categoria==cat]
    b=d[d.es_bci].iloc[0]
    peers=d[~d.es_bci]
    peer_name=st.selectbox('Competidor',peers.fondo.tolist())
    p=peers[peers.fondo==peer_name].iloc[0]
    left,right=st.columns(2)
    for col,row,title in [(left,b,'Fondo BCI'),(right,p,'Peer')]:
        col.subheader(title); col.write(f"**{row.fondo}** · RUN {row.run}")
        col.metric('TE EWMA',pct(row.te_ewma_anual,4)); col.metric('Exceso 1A',pct(row.exceso_1y,2,True)); col.metric('IR',num(row.IR,2,True)); col.metric('Volatilidad',pct(row.vol_anual,2))
    comp=pd.DataFrame({'Métrica':['TE EWMA','Retorno 1A','Exceso 1A','Volatilidad'],'BCI':[b.te_ewma_anual,b.ret_1y_fondo,b.exceso_1y,b.vol_anual],'Peer':[p.te_ewma_anual,p.ret_1y_fondo,p.exceso_1y,p.vol_anual]})
    fig=px.bar(comp,x='Métrica',y=['BCI','Peer'],barmode='group')
    fig.update_layout(yaxis_tickformat='.2%',height=420)
    st.plotly_chart(fig,use_container_width=True)

else:
    st.subheader('Dataset consolidado')
    st.write(f'{len(df)} fondos · {df.categoria.nunique()} categorías · {int(df.es_bci.sum())} fondos BCI')
    st.dataframe(df,use_container_width=True,hide_index=True)
    st.download_button('Descargar CSV',df.to_csv(index=False).encode('utf-8-sig'),'tracking_error_leave_one_out.csv','text/csv')
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as w: df.to_excel(w,index=False,sheet_name='Fondos')
    st.download_button('Descargar Excel',out.getvalue(),'tracking_error_leave_one_out.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

st.markdown('---')
st.caption('BCI Asset Management · Tracking Error Leave-One-Out · datos precalculados del ZIP suministrado')
