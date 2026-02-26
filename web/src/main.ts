import { createApp } from 'vue'
import { createPinia } from 'pinia'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/allura/400.css'
import '@chinese-fonts/maple-mono-cn/dist/MapleMono-CN-Regular/result.css'
import App from './App.vue'
import router from './router'
import './styles/glass.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
