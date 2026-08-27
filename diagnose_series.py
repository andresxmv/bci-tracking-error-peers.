import flask_app as f

for name in ['Asia','Europa','CP Activa']:
    stem,cfg=f.config_by_name(name)
    print('\n===',name,'stem=',stem,'bci=',cfg.get('bci'),'peers=',cfg.get('peers'))
    print('series direct key exists:', stem in f.series)
    levels=f.category_levels(name)
    print('levels shape:', levels.shape)
    print('columns:', list(levels.columns))
    bci,peers=f.configured_columns(name, levels)
    print('matched bci:', bci)
    print('matched peers:', peers)
    h=f.historical_te(name)
    print('chart points:', len(h.get('labels',[])))
    print('first/last:', h.get('labels',[])[:1], h.get('labels',[])[-1:])

print('\nSERIES KEYS:', list(f.series.keys()))
