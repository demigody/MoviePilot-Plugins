export default {
  plugins: [
    {
      postcssPlugin: 'vuetify-federation-filter',
      Once(root) {
        const sourcePath = root.source?.input?.file?.replaceAll('\\\\', '/') || ''
        if (sourcePath.includes('/node_modules/vuetify/') || sourcePath.includes('/node_modules/@mdi/')) {
          root.removeAll()
          return
        }
        root.walkRules(rule => {
          if (rule.selector && (rule.selector.includes('.v-') || rule.selector.includes('.mdi-'))) rule.remove()
        })
      },
    },
  ],
}
