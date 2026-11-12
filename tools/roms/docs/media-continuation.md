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
```

As fixtures exercitam ordem de sincronização, permissões, criação de diretórios, falhas antes e depois da publicação, escrita sobreposta, colisão de temporário, nome longo, preservação de temporário legado e registro com índice adiado. Cada caso usa arquivos pequenos em diretório temporário privado. Falhas de energia e a execução nativa Windows precisam de evidência adicional em ambiente apropriado.

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
