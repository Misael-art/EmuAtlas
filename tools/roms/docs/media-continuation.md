# Continuidade da organização de mídia

## Avanço de baixo consumo

`romtool.atomic_json` usa temporário exclusivo no diretório de destino, preserva as permissões de um arquivo existente, sincroniza os bytes e publica com substituição atômica. Em POSIX, sincroniza também o diretório do arquivo e os vínculos de diretórios criados nessa chamada. Erros de sincronização são propagados ao chamador. Uma falha depois da publicação exige consultar o destino e reconciliar o estado observado.

A implementação Windows sincroniza o arquivo e usa a substituição disponível nesse SO. A cobertura dessa escolha usa simulação em teste. A validação nativa de Windows e do volume compartilhado integra o aceite posterior.

Temporários de chamadas sobrepostas têm nomes exclusivos. A coordenação lógica entre escritores continua a cargo dos locks e das precondições de cada operação. Os movimentos de payload, preimagens e checkpoints por item ainda precisam de sua própria sequência durável.

O registro de artefatos admite adiar a leitura integral do histórico quando o host estiver ocupado.

```sh
python3 tools/roms/romtool.py record-artifacts \
  --change-type tooling-update \
  --reason 'Avanço revisado sob orçamento reduzido' \
  --artifacts-json '["tools/roms/romtool.py"]' \
  --defer-history-index
```

O manifesto e os eventos são registrados imediatamente. O manifesto identifica `history_index_refresh=deferred`. O índice completo pode ser atualizado em uma janela adequada.

```sh
python3 tools/roms/romtool.py history
```

A opção padrão de `record-artifacts` mantém a reconstrução imediata do índice.

## Testes pequenos

```sh
python3 -B -m unittest -v tools.roms.tests.test_atomic_json
python3 -B -m unittest -v tools.roms.tests.test_media_stray_status
```

As fixtures exercitam ordem de sincronização, permissões, criação de diretórios, falhas antes e depois da publicação, escrita sobreposta, colisão de temporário, nome longo, preservação de temporário legado e registro com índice adiado. Cada caso usa arquivos pequenos em diretório temporário privado. Falhas de energia e a execução nativa Windows precisam de evidência adicional em ambiente apropriado.

## Consulta leve da operação interrompida

```sh
python3 tools/roms/romtool.py media stray status --operation-id <operation-id>
```

A consulta lê o manifesto, os eventos locais e os metadados dos caminhos selecionados. O resultado `consistent_metadata` usa código de saída 0. O resultado `reconciliation_required` usa código 3 e inclui os motivos na saída JSON. A evidência identifica `payload_hashes_checked=false` e `global_records_checked=false`, pois essas verificações pertencem à reconciliação integral.

O diagnóstico compara a presença esperada de origem, destino de promoção/quarentena e payload canônico, conforme a ação registrada. Também identifica eventos ilegíveis, registros de outra operação, links ancestrais e caminhos fora do escopo. A observação descreve aquele instante, e mudanças concorrentes exigem nova conferência.

`media stray apply` consulta o diário antes de calcular hashes, criar preimagens ou alterar o manifesto. Um registro de conclusão incompatível com o manifesto impede a reaplicação e orienta a consulta de status. A reconciliação precisa preservar os eventos originais e confirmar os bytes e os registros globais. Depois deve ser preparado um plano atual, respeitando os fingerprints das ferramentas e as demais precondições existentes.

## Checkpoints de movimentos e reversão

O fluxo stray grava a intenção de cada movimento antes do rename, sincroniza os diretórios envolvidos e persiste o resultado de cada item. Uma retomada aceita um destino já presente somente quando a origem está ausente, a intenção registrada coincide com ambos os caminhos e o SHA-256 do destino corresponde ao plano. A conclusão depende da verificação integral dos itens e da publicação durável dos registros correntes.

As preimagens de catálogo, pendências e estado usam cópias CoW quando disponíveis, com cópia de metadados como alternativa nos sistemas de arquivos que exigirem esse procedimento. São registradas assinaturas anteriores e posteriores. A reversão confere essas assinaturas antes de mover payloads e preserva alterações posteriores para reconciliação. Operações legadas que carecem de pós-imagens suficientes exigem reconstruir essa evidência antes de restaurar metadados globais.

Apply e rollback mantêm checkpoints recuperáveis entre o rename e a atualização do item. A publicação repetida dos registros de uma mesma operação mantém uma única entrada corrente de catálogo para cada origem selecionada.

## Sequência da próxima tarefa

1. Ler as instruções do projeto e a evidência local da interrupção. Medir recursos e identificar outras cargas antes de executar verificações ou aplicar planos.
2. Reconciliar origem, destino, quarentena, SHA-256, manifesto, eventos, catálogo e pendências de cada item selecionado. Registrar a recuperação preservando a evidência histórica.
3. Reforçar os fluxos de movimentação existentes com preimagens duráveis, checkpoints por item e retomada testada. Verificar o resultado antes de concluir a operação.
4. Derivar as entradas reais de lançamento e suas dependências a partir do catálogo, runtime e descritores CUE/GDI/M3U. Reutilizar essa resolução na seleção de scraping, associação de mídia e conversões.
5. Confirmar identidade e ordem dos discos e suporte do runtime antes de propor M3U relativo. Coordenar coleções, gamelists, saves e sidecars no plano reversível.
6. Ajustar ES-DE e Skraper para selecionar as entradas de apresentação previstas. Classificar as trilhas dependentes no conjunto do descritor proprietário.
7. Associar mídia geral ao basename da playlist quando ela for a entrada de apresentação. Preservar arte específica dos discos e alternativas. Compartilhar bytes idênticos apenas depois de comparação integral e prova da associação.
8. Integrar compressão pelos perfis lossless aprovados, com verificação lógica e atualização coordenada dos descritores e referências.
9. Concluir os planos preparados de frontends e assets no escopo revisado. Manter o catálogo e a lista global únicos.
10. Comprovar caminhos, aliases e lançamentos no sistema de arquivos de destino, em Linux e Windows. Registrar navegação, mídia, troca de disco, saves e retorno por combinação testada.

## Raízes compartilhadas

- Jogos e descritores usam `roms/<system_id>/`.
- Mídia de jogo usa `media/<system_id>/<media_type>/`.
- Assets criativos usam `frontend/RetroFE/Assets/shared/` e `frontend/RetroFE/Assets/source-artwork/`, compartilhados com consumidores compatíveis em toda a estrutura.
- Dados compartilhados de emuladores usam `emulator/data/`, com binários nas raízes próprias de Linux e Windows.

Referências configuradas e links relativos precisam funcionar no volume escolhido. Toda mudança deve respeitar os planos especializados, a integridade das dependências e o rollback existente.
