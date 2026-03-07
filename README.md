Imagina que o código do projeto é um documento do Google Docs compartilhado com toda a equipe. Se todo mundo editasse ao mesmo tempo, viraria um caos, né?
O branch é como criar uma cópia separada desse documento pra você trabalhar sem atrapalhar ninguém. Quando terminar, você "junta" sua cópia de volta com a principal.
branch main (principal) ────────────────────────────►
                  \
                   branch "feature-login" ───► (QA testa aqui)
                                          \
                                           merge de volta pro main ✅
