s=open('app/static/index.html','r',encoding='utf-8').read()
s2=s
# dropzone
old='<input type="file" id="whSFile" accept=".xlsx">'
if old in s2:
    s2=s2.replace(old,'<div class="dz-row"><input type="file" id="whSFile" class="hidden" accept=".xlsx,.xlsm"><div class="dropzone" id="whSDrop"><span class="dz-label">Перетащите сюда Excel-файл или нажмите для выбора</span></div></div>')
# view menu + paths
if 'whSFromPaths' not in s2:
    s2=s2.replace('<button id="whSFromDisk" class="btn">С диска</button>','<button id="whSFromDisk" class="btn">Импорт с диска</button><button id="whSFromPaths" class="btn" title="Импорт отправок из локальных путей по правилу">Из путей</button>')
if 'whShipmentViewMenu' not in s2:
    s2=s2.replace('<span class="toolbar-right"><span id="whSTablePager"></span></span>','<span class="toolbar-right"><span id="whSTablePager"></span><div class="view-menu" id="whShipmentViewMenu"><button type="button" class="btn" id="btnwhShipmentView">Вид таблицы &#9662;</button><div class="view-menu-panel hidden" id="whShipmentViewPanel"></div></div></span>')
if s2!=s:
    open('app/static/index.html','w',encoding='utf-8').write(s2)
    print('ok')
else:
    print('no')
